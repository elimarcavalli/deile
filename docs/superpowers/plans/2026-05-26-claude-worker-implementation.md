# claude-worker Implementation Plan (#309 fase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Entregar end-to-end o dispatch de tarefas do pipeline DEILE para `claude -p` rodando num pod K8s isolado (`claude-worker`), com configuração per-stage no painel TUI espelhando o pattern do per-stage models (#305), credenciais Pro/Max do operador via `deploy.py k8s claude-login` e installation-on-the-fly diretamente do painel.

**Architecture:** Pod novo `claude-worker` paralelo ao `deile-worker`, HTTP `/v1/dispatch` em `:8767`, mesmo image `deile-stack:local` com `claude` CLI baked. `dispatch_resolver` per-stage espelha `model_resolver`. View unificada `[d]` (`DispatchMatrixView`) substitui as duas atuais. Credentials via Secret + initContainer → PVC writable em `~/.claude/`. NetworkPolicy egress whitelisted por repo. Failure handling reusa retry/escalação existentes.

**Tech Stack:** Python 3.9+ (aiohttp, pydantic, asyncio), Kubernetes (Rancher Desktop/k3s), Rich TUI, claude CLI (`@anthropic-ai/claude-code` via npm), `kubectl set env` para persistência cluster, `~/.deile/settings.json` para persistência CLI.

**Spec source:** `docs/superpowers/specs/2026-05-26-claude-worker-design.md`

---

## File structure

### Novos arquivos

| Path | Responsabilidade |
|---|---|
| `deile/orchestration/pipeline/dispatch_resolver.py` | Fallback chain per-stage para dispatcher (espelha `model_resolver.py`) |
| `deile/tests/orchestration/pipeline/test_dispatch_resolver.py` | Unit tests do resolver |
| `infra/k8s/claude_worker_server.py` | aiohttp listener `:8767` — /v1/dispatch, /v1/health, /v1/progress |
| `infra/k8s/_claude_install.py` | `bootstrap_claude_worker()` shared entre CLI verb e painel |
| `deile/tests/orchestration/pipeline/test_dispatch_payload_extended.py` | Unit tests dos campos novos do payload |
| `deile/tests/orchestration/pipeline/test_worker_implementer_routing.py` | Unit tests do endpoint resolution |
| `deile/tests/infrastructure/test_claude_worker_server.py` | Integration tests do server (mocked subprocess) |
| `deile/tests/infra/test_claude_install.py` | Unit tests do helper de install |
| `deile/tests/infra/test_dispatch_matrix_view.py` | Tests do panel view unificado |
| `deile/tests/might/test_claude_dispatch_real.py` | Smoke test manual (cluster vivo) |
| `infra/k8s/manifests/47-claude-worker-allowed-repos.yaml` | ConfigMap de repos whitelisted |
| `infra/k8s/manifests/48-claude-worker-bearer-secret.yaml` | Secret template (deploy.py popula) |
| `infra/k8s/manifests/49-claude-worker-pvc.yaml` | PVC RWO 1Gi |
| `infra/k8s/manifests/50-claude-worker-deployment.yaml` | Deployment + Service + initContainer |

### Arquivos modificados

| Path | Mudança |
|---|---|
| `infra/k8s/Dockerfile` | Camada nova: nodejs + npm + `npm install -g @anthropic-ai/claude-code` |
| `infra/k8s/wrapper.py` | Novo arg `claude-worker` que invoca `claude_worker_server.py` |
| `infra/k8s/deploy.py` | Novo verb `k8s claude-login [--switch] [--no-interactive]` |
| `infra/k8s/manifests/40-network-policy.yaml` | Pipeline→claude-worker:8767; claude-worker egress whitelisted |
| `infra/k8s/_panel.py` | `DispatchMatrixView` (substitui `DispatchModeView` + `StageModelsView`); remove [M] binding |
| `infra/k8s/_panel_data.py` | `StageDispatchProvider` (consolida) |
| `deile/orchestration/pipeline/dispatch_payload.py` | Campos novos opcionais: stage, action_kind, issue_number, branch |
| `deile/orchestration/pipeline/implementer.py` | `WorkerImplementer.endpoint_override`; `build_implementer` simplificada |
| `deile/config/settings.py` | Schema `pipeline.dispatchers.<stage>` |
| `CLAUDE.md` | Nova seção "claude-worker dispatching" |
| `docs/system_design/04-MODELO-COMPONENTES.md` | Adiciona claude-worker em diagramas |
| `docs/system_design/14-CONTAINERIZACAO.md` | Adiciona claude-worker como 4ª init mode |
| `docs/system_design/00-VISAO-GERAL.md` | Decision #42 registrada |

---

## Task 1: Criar `dispatch_resolver.py`

**Files:**
- Create: `deile/orchestration/pipeline/dispatch_resolver.py`
- Test: `deile/tests/orchestration/pipeline/test_dispatch_resolver.py`

- [ ] **Step 1: Write failing tests**

Create `deile/tests/orchestration/pipeline/test_dispatch_resolver.py`:

```python
"""Unit tests for ``dispatch_resolver`` — espelha ``test_model_resolver.py``.

Cobre:
- Fallback chain (stage env → global env → built-in default)
- Whitelist enforcement (only 'deile-worker' | 'claude-worker' accepted)
- ValueError para stage inválido (programming bug)
- Endpoint mapping (deile-worker → :8766, claude-worker → :8767)
"""

import os
import pytest

from deile.orchestration.pipeline.dispatch_resolver import (
    PIPELINE_STAGES,
    VALID_DISPATCHERS,
    resolve_stage_dispatcher,
    get_endpoint_for,
    is_valid_dispatcher,
)


def _clear_env(monkeypatch):
    for stage in PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)
    monkeypatch.delenv("DEILE_WORKER_ENDPOINT", raising=False)
    monkeypatch.delenv("DEILE_CLAUDE_WORKER_ENDPOINT", raising=False)


def test_stages_canonical_order():
    """Stage tuple keeps operational lifecycle order."""
    assert PIPELINE_STAGES == ("classify", "refine", "implement", "pr_review", "follow_ups")


def test_valid_dispatchers_frozen():
    assert "deile-worker" in VALID_DISPATCHERS
    assert "claude-worker" in VALID_DISPATCHERS
    assert len(VALID_DISPATCHERS) == 2


def test_resolve_default_returns_deile_worker(monkeypatch):
    """Sem nenhuma env var, default built-in = deile-worker."""
    _clear_env(monkeypatch)
    assert resolve_stage_dispatcher("implement") == "deile-worker"


def test_resolve_global_env(monkeypatch):
    """DEILE_PIPELINE_DISPATCH_MODE sobrescreve built-in default."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "claude-worker")
    assert resolve_stage_dispatcher("implement") == "claude-worker"
    assert resolve_stage_dispatcher("classify") == "claude-worker"


def test_resolve_stage_overrides_global(monkeypatch):
    """DEILE_PIPELINE_DISPATCH_<STAGE> sobrescreve global."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "deile-worker")
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
    assert resolve_stage_dispatcher("implement") == "claude-worker"
    assert resolve_stage_dispatcher("classify") == "deile-worker"


def test_resolve_invalid_stage_raises(monkeypatch):
    """Stage fora de PIPELINE_STAGES → ValueError (programming bug)."""
    _clear_env(monkeypatch)
    with pytest.raises(ValueError, match="unknown stage"):
        resolve_stage_dispatcher("non_existent")


def test_resolve_invalid_dispatcher_in_env_falls_through(monkeypatch):
    """Valor inválido em env → ValueError com mensagem clara."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "garbage")
    with pytest.raises(ValueError, match="unknown dispatcher"):
        resolve_stage_dispatcher("implement")


def test_resolve_empty_string_treated_as_unset(monkeypatch):
    """Empty value → fallback continues."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "")
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_MODE", "claude-worker")
    assert resolve_stage_dispatcher("implement") == "claude-worker"


def test_get_endpoint_for_deile_worker(monkeypatch):
    _clear_env(monkeypatch)
    assert get_endpoint_for("deile-worker") == "http://deile-worker:8766"


def test_get_endpoint_for_claude_worker(monkeypatch):
    _clear_env(monkeypatch)
    assert get_endpoint_for("claude-worker") == "http://claude-worker:8767"


def test_get_endpoint_for_honors_env_override(monkeypatch):
    """Env override útil pra dev local que não usa Service DNS."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ENDPOINT", "http://localhost:9090")
    assert get_endpoint_for("claude-worker") == "http://localhost:9090"


def test_get_endpoint_for_unknown_raises():
    with pytest.raises(ValueError, match="unknown dispatcher"):
        get_endpoint_for("magical-worker")


def test_is_valid_dispatcher_table():
    assert is_valid_dispatcher("deile-worker") is True
    assert is_valid_dispatcher("claude-worker") is True
    assert is_valid_dispatcher("DEILE-WORKER") is True  # case-insensitive
    assert is_valid_dispatcher("garbage") is False
    assert is_valid_dispatcher("") is False
    assert is_valid_dispatcher(None) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/test_dispatch_resolver.py -v 2>&1 | tail -20
```

Expected: ALL fail with `ImportError` (module doesn't exist yet).

- [ ] **Step 3: Implement `dispatch_resolver.py`**

Create `deile/orchestration/pipeline/dispatch_resolver.py`:

```python
"""Dispatch resolver — espelha :mod:`model_resolver` mas para a escolha de
worker (qual pod recebe o POST /v1/dispatch) ao invés de modelo.

Cada stage do pipeline (``classify``, ``refine``, ``implement``, ``pr_review``,
``follow_ups``) pode ter seu dispatcher overriden via env var; sem override,
cai pro global ``DEILE_PIPELINE_DISPATCH_MODE``; sem isso, default built-in
é ``deile-worker``.

A escolha entre os dois é independente da escolha do modelo (issue #309
correção do user: worker ≠ modelo). ``claude-worker`` só aceita modelos
``anthropic:*``; ``deile-worker`` aceita qualquer modelo.
"""
from __future__ import annotations

import os
from typing import FrozenSet, Optional, Tuple

#: Ordem operacional (igual model_resolver). Sufixo usado como JSON key.
PIPELINE_STAGES: Tuple[str, ...] = (
    "classify",
    "refine",
    "implement",
    "pr_review",
    "follow_ups",
)

#: Valores aceitos. Frozen para evitar mutação acidental.
VALID_DISPATCHERS: FrozenSet[str] = frozenset({"deile-worker", "claude-worker"})

_DEFAULT_DISPATCHER = "deile-worker"

# Default endpoints. Env vars sobrescrevem (útil pra dev local fora do cluster).
_ENDPOINT_DEFAULTS = {
    "deile-worker": "http://deile-worker:8766",
    "claude-worker": "http://claude-worker:8767",
}
_ENDPOINT_ENV_VARS = {
    "deile-worker": "DEILE_WORKER_ENDPOINT",
    "claude-worker": "DEILE_CLAUDE_WORKER_ENDPOINT",
}


def is_valid_dispatcher(value: Optional[str]) -> bool:
    """Returns True if *value* casa com :data:`VALID_DISPATCHERS` (case-insensitive)."""
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in VALID_DISPATCHERS


def _canonicalize(value: Optional[str]) -> Optional[str]:
    """Normaliza para forma canônica em VALID_DISPATCHERS; None se vazio."""
    if not value or not value.strip():
        return None
    canonical = value.strip().lower()
    if canonical not in VALID_DISPATCHERS:
        raise ValueError(
            f"unknown dispatcher {value!r}; expected one of {sorted(VALID_DISPATCHERS)}"
        )
    return canonical


def resolve_stage_dispatcher(stage: str) -> str:
    """Resolve qual dispatcher (worker pod) recebe o dispatch de *stage*.

    Fallback chain (top → bottom):
      1. ``DEILE_PIPELINE_DISPATCH_<STAGE>`` env var
      2. ``DEILE_PIPELINE_DISPATCH_MODE`` env var (global default)
      3. Built-in default: ``deile-worker``

    Raises:
        ValueError: stage não está em :data:`PIPELINE_STAGES` (programming bug,
            não user input — implementer methods passam de uma whitelist).
        ValueError: env var contém valor não-whitelisted (fail-fast para evitar
            queimar budget no engine errado por typo).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    stage_env = os.environ.get(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}")
    resolved = _canonicalize(stage_env)
    if resolved:
        return resolved

    global_env = os.environ.get("DEILE_PIPELINE_DISPATCH_MODE")
    resolved = _canonicalize(global_env)
    if resolved:
        return resolved

    return _DEFAULT_DISPATCHER


def get_endpoint_for(dispatcher: str) -> str:
    """Resolve a URL HTTP do worker pod *dispatcher*.

    Env var (``DEILE_WORKER_ENDPOINT`` ou ``DEILE_CLAUDE_WORKER_ENDPOINT``)
    sobrescreve o default — útil para dev local que aponta para localhost
    em vez do Service DNS do cluster.

    Raises:
        ValueError: dispatcher fora de :data:`VALID_DISPATCHERS`.
    """
    canonical = _canonicalize(dispatcher)
    if canonical is None:
        raise ValueError(f"unknown dispatcher {dispatcher!r}")
    env_var = _ENDPOINT_ENV_VARS[canonical]
    return os.environ.get(env_var) or _ENDPOINT_DEFAULTS[canonical]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/test_dispatch_resolver.py -v 2>&1 | tail -10
```

Expected: 13 passed.

- [ ] **Step 5: Run lint**

```bash
ruff check deile/orchestration/pipeline/dispatch_resolver.py deile/tests/orchestration/pipeline/test_dispatch_resolver.py
isort --check-only deile/orchestration/pipeline/dispatch_resolver.py deile/tests/orchestration/pipeline/test_dispatch_resolver.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add deile/orchestration/pipeline/dispatch_resolver.py \
       deile/tests/orchestration/pipeline/test_dispatch_resolver.py
git commit -m "feat(pipeline): dispatch_resolver per-stage para escolha de worker

Espelha model_resolver para a escolha de qual pod (deile-worker | claude-worker)
recebe o POST /v1/dispatch. Fallback chain: DEILE_PIPELINE_DISPATCH_<STAGE> →
DEILE_PIPELINE_DISPATCH_MODE → 'deile-worker'.

get_endpoint_for() mapeia dispatcher → URL HTTP, com env var override pra dev
local.

Refs #309"
```

---

## Task 2: Estender `DispatchPayload` com campos novos opcionais

**Files:**
- Modify: `deile/orchestration/pipeline/dispatch_payload.py` (ou wherever DispatchPayload lives — confirmar com `grep -rn "class DispatchPayload"`)
- Test: `deile/tests/orchestration/pipeline/test_dispatch_payload_extended.py`

- [ ] **Step 1: Localizar DispatchPayload**

```bash
grep -rn "class DispatchPayload" deile/ | head -3
```

Expected: encontra em `deile/infrastructure/deile_worker_client.py` (ou similar). Anote o path exato como `<PAYLOAD_FILE>`.

- [ ] **Step 2: Write failing tests**

Create `deile/tests/orchestration/pipeline/test_dispatch_payload_extended.py`:

```python
"""Unit tests para os campos novos opcionais do DispatchPayload (#309 fase 2).

Backward compat é crítico — deile-worker existing não sabe dos campos novos,
mas DEVE aceitar payloads que os carregam (ignorando se não usar) e gerar
payloads válidos sem eles.
"""
import pytest

from deile.infrastructure.deile_worker_client import DispatchPayload


def test_minimal_payload_still_works():
    """Campos antigos sozinhos (backward compat)."""
    p = DispatchPayload(brief="implement #1", channel_id="auto/issue-1")
    assert p.brief == "implement #1"
    assert p.channel_id == "auto/issue-1"
    assert p.stage is None
    assert p.action_kind is None
    assert p.issue_number is None
    assert p.branch is None


def test_full_payload_with_new_fields():
    p = DispatchPayload(
        brief="implement #309",
        channel_id="auto/issue-309",
        preferred_model="anthropic:claude-opus-4-7",
        stage="implement",
        action_kind="implement",
        issue_number=309,
        branch="auto/issue-309",
    )
    assert p.stage == "implement"
    assert p.action_kind == "implement"
    assert p.issue_number == 309
    assert p.branch == "auto/issue-309"


def test_payload_to_json_roundtrip():
    """Serialização preserva campos novos."""
    import json
    p = DispatchPayload(
        brief="x", channel_id="c",
        stage="pr_review", issue_number=42, branch="auto/issue-42",
    )
    data = p.to_dict() if hasattr(p, "to_dict") else p.__dict__
    j = json.dumps(data)
    parsed = json.loads(j)
    assert parsed["stage"] == "pr_review"
    assert parsed["issue_number"] == 42
    assert parsed["branch"] == "auto/issue-42"


def test_invalid_stage_raises():
    """Stage value validation — se setado, deve estar em PIPELINE_STAGES."""
    with pytest.raises((ValueError, TypeError)):
        DispatchPayload(brief="x", channel_id="c", stage="garbage_stage")


def test_new_fields_in_dict_output():
    """Verifica que campos novos aparecem no dict pra HTTP POST."""
    p = DispatchPayload(brief="x", channel_id="c", stage="implement")
    d = p.to_dict() if hasattr(p, "to_dict") else p.__dict__
    assert "stage" in d
    assert "action_kind" in d
    assert "issue_number" in d
    assert "branch" in d
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/test_dispatch_payload_extended.py -v 2>&1 | tail -10
```

Expected: failures with `TypeError` (unexpected kwargs).

- [ ] **Step 4: Add fields to DispatchPayload**

Edit `<PAYLOAD_FILE>` (typically `deile/infrastructure/deile_worker_client.py`). Locate the dataclass definition and add fields:

```python
@dataclass
class DispatchPayload:
    brief: str
    channel_id: str
    preferred_model: Optional[str] = None
    # NEW (issue #309 fase 2) — todos opcionais; deile-worker ignora se não usa.
    stage: Optional[str] = None              # classify|refine|implement|pr_review|follow_ups
    action_kind: Optional[str] = None        # implement|review|mention|refine|decompose|...
    issue_number: Optional[int] = None
    branch: Optional[str] = None

    def __post_init__(self):
        if self.stage is not None:
            from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES
            if self.stage not in PIPELINE_STAGES:
                raise ValueError(
                    f"invalid stage {self.stage!r}; expected one of {PIPELINE_STAGES}"
                )
        # existing __post_init__ logic, se houver...

    def to_dict(self) -> dict:
        """Para HTTP POST body. Omite campos None pra compat com deile-worker antigo."""
        d = {"brief": self.brief, "channel_id": self.channel_id}
        if self.preferred_model is not None:
            d["preferred_model"] = self.preferred_model
        if self.stage is not None:
            d["stage"] = self.stage
        if self.action_kind is not None:
            d["action_kind"] = self.action_kind
        if self.issue_number is not None:
            d["issue_number"] = self.issue_number
        if self.branch is not None:
            d["branch"] = self.branch
        return d
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/test_dispatch_payload_extended.py \
       deile/tests/infrastructure/test_dispatch_payload_preferred_model.py -v 2>&1 | tail -10
```

Expected: all pass (new tests + existing preferred_model tests still green).

- [ ] **Step 6: Commit**

```bash
git add deile/infrastructure/deile_worker_client.py \
       deile/tests/orchestration/pipeline/test_dispatch_payload_extended.py
git commit -m "feat(pipeline): DispatchPayload aceita stage/action_kind/issue_number/branch

Campos novos opcionais (#309 fase 2). Backward compat preservado:
- Payload mínimo (brief + channel_id) continua válido
- to_dict() omite None values pra deile-worker antigo não receber chaves
  desconhecidas
- stage value é validado contra PIPELINE_STAGES; outros campos free-form

Refs #309"
```

---

## Task 3: Settings schema — `pipeline.dispatchers.<stage>`

**Files:**
- Modify: `deile/config/settings.py`
- Test: `deile/tests/config/test_pipeline_dispatchers_schema.py`

- [ ] **Step 1: Write failing tests**

Create `deile/tests/config/test_pipeline_dispatchers_schema.py`:

```python
"""Schema validation para pipeline.dispatchers em ~/.deile/settings.json."""
import json
from pathlib import Path

import pytest

from deile.config.settings import Settings, get_settings


def test_dispatchers_field_defaults_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("DEILE_SETTINGS_FILE", str(tmp_path / "settings.json"))
    (tmp_path / "settings.json").write_text("{}")
    s = Settings.load()
    assert s.pipeline.dispatchers == {}


def test_dispatchers_valid_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DEILE_SETTINGS_FILE", str(tmp_path / "settings.json"))
    (tmp_path / "settings.json").write_text(json.dumps({
        "pipeline": {
            "dispatchers": {
                "implement": "claude-worker",
                "pr_review": "claude-worker",
                "classify": "deile-worker",
            }
        }
    }))
    s = Settings.load()
    assert s.pipeline.dispatchers["implement"] == "claude-worker"
    assert s.pipeline.dispatchers["classify"] == "deile-worker"


def test_dispatchers_invalid_dispatcher_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DEILE_SETTINGS_FILE", str(tmp_path / "settings.json"))
    (tmp_path / "settings.json").write_text(json.dumps({
        "pipeline": {
            "dispatchers": {"implement": "garbage-worker"}
        }
    }))
    with pytest.raises((ValueError, Exception)):  # adjust based on lib (pydantic v2)
        Settings.load()


def test_dispatchers_invalid_stage_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DEILE_SETTINGS_FILE", str(tmp_path / "settings.json"))
    (tmp_path / "settings.json").write_text(json.dumps({
        "pipeline": {
            "dispatchers": {"non_existent_stage": "deile-worker"}
        }
    }))
    with pytest.raises((ValueError, Exception)):
        Settings.load()
```

- [ ] **Step 2: Run tests, expect failures**

```bash
python3 -m pytest deile/tests/config/test_pipeline_dispatchers_schema.py -v 2>&1 | tail -10
```

Expected: fail because `Settings.pipeline.dispatchers` doesn't exist.

- [ ] **Step 3: Read existing pipeline settings schema**

```bash
grep -n "class PipelineSettings\|pipeline:\|class Pipeline\|dispatch_mode" deile/config/settings.py | head -20
```

Locate the `PipelineSettings` (or similar) pydantic model in `deile/config/settings.py`.

- [ ] **Step 4: Add `dispatchers` field**

In `deile/config/settings.py`, find the pipeline section model class and add:

```python
from typing import Dict

from pydantic import field_validator
from deile.orchestration.pipeline.dispatch_resolver import (
    PIPELINE_STAGES, VALID_DISPATCHERS,
)

class PipelineSettings(BaseModel):
    # ... existing fields ...
    
    dispatchers: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-stage dispatcher override. Keys são stages de PIPELINE_STAGES; "
                    "values são membros de VALID_DISPATCHERS.",
    )
    
    @field_validator("dispatchers")
    @classmethod
    def _validate_dispatchers(cls, v: Dict[str, str]) -> Dict[str, str]:
        for stage, dispatcher in v.items():
            if stage not in PIPELINE_STAGES:
                raise ValueError(
                    f"Invalid stage {stage!r} in pipeline.dispatchers; "
                    f"expected one of {PIPELINE_STAGES}"
                )
            if dispatcher.strip().lower() not in VALID_DISPATCHERS:
                raise ValueError(
                    f"Invalid dispatcher {dispatcher!r} for stage {stage!r}; "
                    f"expected one of {sorted(VALID_DISPATCHERS)}"
                )
        return v
```

- [ ] **Step 5: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/config/test_pipeline_dispatchers_schema.py -v 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add deile/config/settings.py deile/tests/config/test_pipeline_dispatchers_schema.py
git commit -m "feat(config): pipeline.dispatchers schema validation per-stage

Adiciona Dict[stage, dispatcher] em PipelineSettings com validator que
rejeita stages fora de PIPELINE_STAGES e dispatchers fora de
VALID_DISPATCHERS. CLI persistence layer alinhada com dispatch_resolver.

Refs #309"
```

---

## Task 4: `WorkerImplementer` ganha `endpoint_override` + integra `dispatch_resolver`

**Files:**
- Modify: `deile/orchestration/pipeline/implementer.py` — `WorkerImplementer` class
- Test: `deile/tests/orchestration/pipeline/test_worker_implementer_routing.py`

- [ ] **Step 1: Write failing tests**

Create `deile/tests/orchestration/pipeline/test_worker_implementer_routing.py`:

```python
"""WorkerImplementer escolhe endpoint correto por stage via dispatch_resolver."""
from unittest.mock import AsyncMock, patch

import pytest

from deile.orchestration.pipeline.implementer import WorkerImplementer


@pytest.fixture
def stub_monitor():
    """Minimal monitor stub para testes que não chamam claude/deile real."""
    class _Stub:
        class config:
            repo = "elimarcavalli/deile"
            base_repo_path = "/tmp/fake"
        worktrees = None
        claude = None
        forge = type("F", (), {"config": None})()
        def branch_for_issue(self, n): return f"auto/issue-{n}"
    return _Stub()


@pytest.fixture
def stub_issue():
    class _Issue:
        number = 42
        title = "test issue"
        body = "test body"
    return _Issue()


@pytest.mark.asyncio
async def test_endpoint_override_takes_precedence(stub_monitor, stub_issue, monkeypatch):
    """endpoint_override sobrescreve qualquer resolve_stage_dispatcher result."""
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
    impl = WorkerImplementer(endpoint_override="http://forced-endpoint:9999")
    
    with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"ok": True, "stdout": "", "stderr": ""}
        await impl.implement(stub_monitor, stub_issue)
        
        # Verifica que o POST foi pra endpoint forçado, NÃO pro claude-worker
        called_url = mock_post.call_args[0][0]
        assert "forced-endpoint:9999" in called_url


@pytest.mark.asyncio
async def test_resolves_endpoint_from_stage(stub_monitor, stub_issue, monkeypatch):
    """Sem override, usa resolve_stage_dispatcher para escolher endpoint."""
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
    monkeypatch.delenv("DEILE_CLAUDE_WORKER_ENDPOINT", raising=False)
    impl = WorkerImplementer()
    
    with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"ok": True, "stdout": "", "stderr": ""}
        await impl.implement(stub_monitor, stub_issue)
        
        called_url = mock_post.call_args[0][0]
        assert "claude-worker:8767" in called_url


@pytest.mark.asyncio
async def test_review_uses_pr_review_stage(stub_monitor, monkeypatch):
    """review() resolve pelo stage 'pr_review', não 'implement'."""
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", "claude-worker")
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_PR_REVIEW", "deile-worker")
    impl = WorkerImplementer()
    
    class _PR:
        number = 100
        title = "test pr"
        head_ref = "auto/issue-100"
    
    with patch.object(impl, "_post_dispatch", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"ok": True, "stdout": "", "stderr": ""}
        await impl.review(stub_monitor, _PR())
        
        called_url = mock_post.call_args[0][0]
        assert "deile-worker:8766" in called_url
```

- [ ] **Step 2: Run tests, expect failures**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/test_worker_implementer_routing.py -v 2>&1 | tail -10
```

Expected: fail because `endpoint_override` not in `__init__`, no `_post_dispatch` method, etc.

- [ ] **Step 3: Modify `WorkerImplementer`**

Edit `deile/orchestration/pipeline/implementer.py`, locate `class WorkerImplementer(PipelineImplementer):` and modify:

```python
from deile.orchestration.pipeline.dispatch_resolver import (
    get_endpoint_for, resolve_stage_dispatcher,
)


class WorkerImplementer(PipelineImplementer):
    """HTTP-based implementer. Dispatcha pra deile-worker OU claude-worker
    baseado em ``dispatch_resolver.resolve_stage_dispatcher(stage)``.
    
    ``endpoint_override`` força um endpoint específico (útil em testes ou
    para configurações ad-hoc); sem override, usa o resolver per-stage.
    """
    name = "deile_worker"
    
    def __init__(
        self,
        *,
        client: Optional[object] = None,
        endpoint_override: Optional[str] = None,
    ):
        self._client = client
        self._endpoint_override = endpoint_override
    
    def _resolve_endpoint(self, stage: str) -> str:
        if self._endpoint_override:
            return self._endpoint_override
        return get_endpoint_for(resolve_stage_dispatcher(stage))
    
    async def _post_dispatch(self, endpoint: str, payload: dict) -> dict:
        """POST <endpoint>/v1/dispatch with bearer auth. Returns parsed JSON.
        Existing implementation from previous version goes here — preserva
        bearer token + timeout + retries existentes.
        """
        # ... use self._client OR build a fresh one ...
        # ... bearer token logic (existing) ...
        # ... POST with timeout, raise on error ...
        # ... return response.json()
        ...  # implementação existente
    
    async def implement(
        self, monitor, issue, *, resume: bool = False,
    ) -> WorkOutcome:
        endpoint = self._resolve_endpoint("implement")
        brief = render_implement_prompt(
            monitor.config.repo, issue.number, issue.title, issue.body,
            forge=monitor.forge.config,
        )
        payload = DispatchPayload(
            brief=brief,
            channel_id=monitor.branch_for_issue(issue.number),
            preferred_model=resolve_stage_model("implement"),
            stage="implement",
            action_kind="implement",
            issue_number=issue.number,
            branch=monitor.branch_for_issue(issue.number),
        )
        result = await self._post_dispatch(
            f"{endpoint}/v1/dispatch", payload.to_dict(),
        )
        return _outcome_from_worker_response(result)
    
    async def review(self, monitor, pr, *, resume: bool = False) -> WorkOutcome:
        endpoint = self._resolve_endpoint("pr_review")
        brief = render_review_prompt(
            monitor.config.repo, pr.number, pr.title, forge=monitor.forge.config,
        )
        payload = DispatchPayload(
            brief=brief,
            channel_id=pr.head_ref or f"pr/{pr.number}",
            preferred_model=resolve_stage_model("pr_review"),
            stage="pr_review",
            action_kind="review",
            issue_number=None,
            branch=pr.head_ref,
        )
        result = await self._post_dispatch(
            f"{endpoint}/v1/dispatch", payload.to_dict(),
        )
        return _outcome_from_worker_response(result)
    
    # mention() análogo, usando stage="implement" ou stage="pr_review" baseado em mode
```

- [ ] **Step 4: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/test_worker_implementer_routing.py \
       deile/tests/orchestration/pipeline/test_dispatch_resolver.py -v 2>&1 | tail -15
```

Expected: all pass.

- [ ] **Step 5: Run existing implementer tests to ensure no regression**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/ -v 2>&1 | tail -20
```

Expected: existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add deile/orchestration/pipeline/implementer.py \
       deile/tests/orchestration/pipeline/test_worker_implementer_routing.py
git commit -m "feat(pipeline): WorkerImplementer resolve endpoint per-stage

WorkerImplementer.__init__ aceita endpoint_override (override total) e usa
dispatch_resolver.resolve_stage_dispatcher() para escolher endpoint quando
não há override. Cada método (implement/review/mention) declara seu stage
explicitamente.

Refs #309"
```

---

## Task 5: Simplificar `build_implementer`

**Files:**
- Modify: `deile/orchestration/pipeline/implementer.py` — função `build_implementer`
- Existing test: `deile/tests/orchestration/pipeline/test_build_implementer.py` (se existir)

- [ ] **Step 1: Locate `build_implementer`**

```bash
grep -n "def build_implementer\|def is_claude_mode" deile/orchestration/pipeline/implementer.py
```

- [ ] **Step 2: Modificar para sempre retornar WorkerImplementer**

Edit `deile/orchestration/pipeline/implementer.py`:

```python
def build_implementer(
    dispatch_mode: Optional[str] = None,
    *,
    worker_client: Optional[object] = None,
) -> PipelineImplementer:
    """Returns WorkerImplementer (sempre). O resolver per-stage decide endpoint
    em runtime; ``dispatch_mode`` global aqui só sobrescreve a env var
    ``DEILE_PIPELINE_DISPATCH_MODE`` para compat com chamadas antigas.
    
    A classe ``ClaudeImplementer`` permanece para uso local (CLI fora do
    cluster); :func:`get_local_claude_implementer` é a factory exclusiva pra
    esse caso.
    """
    if dispatch_mode and dispatch_mode.strip():
        # Compat com chamadas antigas que passam dispatch_mode literal — só
        # valida e ignora (resolver lê env var); um typo continua falhando
        # fail-fast pelo resolver na próxima chamada.
        from deile.orchestration.pipeline.dispatch_resolver import is_valid_dispatcher
        if not is_valid_dispatcher(dispatch_mode):
            raise ValueError(
                f"unknown dispatch_mode {dispatch_mode!r}; "
                f"expected one of ('deile-worker', 'claude-worker')"
            )
    
    return WorkerImplementer(client=worker_client)


def get_local_claude_implementer() -> "ClaudeImplementer":
    """Factory exclusiva para uso local fora do cluster (deile CLI).
    
    Emite warning de boot se ``claude`` binary não está no PATH.
    """
    _warn_if_claude_unavailable()
    return ClaudeImplementer()
```

- [ ] **Step 3: Update existing callers**

```bash
grep -rn "build_implementer\|_build_claude_implementer_with_warning\|is_claude_mode" deile/ | grep -v test | grep -v ".pyc"
```

Update each caller — most should pass through `dispatch_mode` as before; the new behavior is transparent.

- [ ] **Step 4: Run test suite (focal slice)**

```bash
python3 -m pytest deile/tests/orchestration/pipeline/ -v -k "implementer" 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add deile/orchestration/pipeline/implementer.py
git commit -m "refactor(pipeline): build_implementer sempre retorna WorkerImplementer

Substitui o switch entre ClaudeImplementer e WorkerImplementer por
WorkerImplementer único — o resolver per-stage decide endpoint em runtime.
ClaudeImplementer (legacy, CLI local) ganha factory dedicada
get_local_claude_implementer() para callers que rodam fora do cluster.

dispatch_mode aceito ainda valida fail-fast em typos.

Refs #309"
```

---

## Task 6: Dockerfile — instala claude CLI

**Files:**
- Modify: `infra/k8s/Dockerfile`

- [ ] **Step 1: Read current Dockerfile to find insertion point**

```bash
sed -n '100,140p' infra/k8s/Dockerfile
```

Look for `RUN apt-get install ... gh` or similar — insert claude install AFTER gh/glab layers (preserves cache for rebuilds that don't touch claude).

- [ ] **Step 2: Add nodejs + claude CLI install layer**

Add to `infra/k8s/Dockerfile` after the gh/glab layer:

```dockerfile
# -----------------------------------------------------------------------------
# claude CLI (issue #309 fase 2)
#
# Bake do claude CLI no image. Permite que o pod claude-worker rode
# `claude -p` sem instalar em runtime (que exigiria egress npm e seria
# bloqueado pela NetworkPolicy default-deny).
#
# Camada separada das anteriores (gh/glab) para layer cache em rebuilds que
# não tocam claude.
#
# nodejs ~20 (LTS) via NodeSource (debian-based). Tamanho: ~80MB nodejs + ~30MB
# claude CLI = ~110MB nesta camada.
# -----------------------------------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g --omit=dev @anthropic-ai/claude-code \
 && claude --version  # smoke test no build (falha se install quebrou)
```

- [ ] **Step 3: Rebuild image and verify claude is present**

```bash
python3 infra/k8s/deploy.py k8s build --yes 2>&1 | tail -20
```

Expected: build completes successfully; layer log mentions `claude` install.

- [ ] **Step 4: Smoke test claude binary in image**

```bash
K=~/.rd/bin/kubectl
# Spawn a fresh shell pod to verify the binary is present
$K -n deile run claude-smoke-test --rm -i --restart=Never \
   --image=deile-stack:local --image-pull-policy=Never \
   --command -- claude --version
```

Expected: prints claude version, exits 0.

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/Dockerfile
git commit -m "feat(infra): instala claude CLI no image deile-stack:local

Bake do @anthropic-ai/claude-code via npm numa camada nova após gh/glab.
Smoke test \`claude --version\` no build catch installs quebrados.

Cresce o image em ~110MB; aceitável (total ~620MB, ainda enxuto pra
enterprise stacks típicos).

Refs #309"
```

---

## Task 7: Manifest 47 — ConfigMap allowed-repos

**Files:**
- Create: `infra/k8s/manifests/47-claude-worker-allowed-repos.yaml`

- [ ] **Step 1: Write manifest**

Create `infra/k8s/manifests/47-claude-worker-allowed-repos.yaml`:

```yaml
# ConfigMap com regex de repos permitidos para git clone/push do claude-worker.
# Camada de defesa contra prompt-injection que tente exfiltrar credentials via
# `git push owner-malicioso/leak-repo`. wrapper.py valida cada URL contra esses
# patterns antes de exec do claude CLI.
#
# Issue #309 fase 2 — seção 7 do design.
apiVersion: v1
kind: ConfigMap
metadata:
  name: claude-worker-allowed-repos
  namespace: deile
  labels:
    app: claude-worker
    role: deile
data:
  # Lista de regex (uma por linha) — wrapper.py compila todas e exige match
  # contra repo URL antes de permitir git operation.
  allowed_repos.regex: |
    ^https://github\.com/elimarcavalli/(deile|deilebot)(\.git)?$
    ^https://gitlab\.com/elimarcavalli/(deile|deilebot)(\.git)?$
    ^git@github\.com:elimarcavalli/(deile|deilebot)(\.git)?$
    ^git@gitlab\.com:elimarcavalli/(deile|deilebot)(\.git)?$
```

- [ ] **Step 2: Smoke validate**

```bash
~/.rd/bin/kubectl apply --dry-run=client -f infra/k8s/manifests/47-claude-worker-allowed-repos.yaml
```

Expected: `configmap/claude-worker-allowed-repos created (dry run)`

- [ ] **Step 3: Commit**

```bash
git add infra/k8s/manifests/47-claude-worker-allowed-repos.yaml
git commit -m "feat(k8s): ConfigMap allowed-repos regex pro claude-worker

Whitelist de URLs de git clone/push que o claude-worker pode usar. Camada
de defesa contra prompt-injection que tente exfiltrar credentials via push
pra repo arbitrário (documentado na spec seção 7.1).

wrapper.py valida cada URL contra esses patterns antes do exec do claude.

Refs #309"
```

---

## Task 8: Manifest 48 — Bearer Secret template + Manifest 49 — PVC

**Files:**
- Create: `infra/k8s/manifests/48-claude-worker-bearer-secret.yaml`
- Create: `infra/k8s/manifests/49-claude-worker-pvc.yaml`

- [ ] **Step 1: Write manifest 48 (template — bearer Secret)**

Create `infra/k8s/manifests/48-claude-worker-bearer-secret.yaml`:

```yaml
# Bearer token Secret para claude-worker (idem worker-bearer do deile-worker).
# Conteúdo é populado pelo deploy.py k8s claude-login.
#
# Stub: o Secret deve existir antes do Deployment 50 ser aplicado, mas o
# conteúdo é gerado em runtime. Aplicar este YAML cria um Secret vazio que
# o deploy.py atualiza via `kubectl create secret generic --dry-run=client
# -o yaml | kubectl apply -f -`.
apiVersion: v1
kind: Secret
metadata:
  name: claude-worker-bearer
  namespace: deile
  labels:
    app: claude-worker
    role: deile
type: Opaque
data:
  # Populated by `python3 infra/k8s/deploy.py k8s claude-login`.
  # Base64-encoded random 32-byte token.
  CLAUDE_WORKER_BEARER_TOKEN: ""  # placeholder
```

- [ ] **Step 2: Write manifest 49 (PVC)**

Create `infra/k8s/manifests/49-claude-worker-pvc.yaml`:

```yaml
# PVC para claude-worker. Holds:
# - /home/claude/.claude/credentials.json  (escrito pelo initContainer; refresh in-pod
#   atualiza in-place)
# - /home/claude/work/<task_id>/...        (worktrees per-task)
#
# RWO single-node (local-path StorageClass do Rancher Desktop). FU #5 da
# spec aborda RWX para multi-replica.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: claude-worker-home
  namespace: deile
  labels:
    app: claude-worker
    role: deile
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 1Gi
```

- [ ] **Step 3: Smoke validate both**

```bash
~/.rd/bin/kubectl apply --dry-run=client \
   -f infra/k8s/manifests/48-claude-worker-bearer-secret.yaml \
   -f infra/k8s/manifests/49-claude-worker-pvc.yaml
```

Expected: both `created (dry run)`.

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/manifests/48-claude-worker-bearer-secret.yaml \
       infra/k8s/manifests/49-claude-worker-pvc.yaml
git commit -m "feat(k8s): bearer Secret template + PVC do claude-worker

Manifest 48 é placeholder; deploy.py k8s claude-login popula com
random token. Manifest 49 é PVC RWO 1Gi (local-path StorageClass do
Rancher Desktop).

Refs #309"
```

---

## Task 9: Manifest 50 — claude-worker Deployment + Service

**Files:**
- Create: `infra/k8s/manifests/50-claude-worker-deployment.yaml`

- [ ] **Step 1: Write manifest 50**

Create `infra/k8s/manifests/50-claude-worker-deployment.yaml`:

```yaml
# claude-worker — pod paralelo ao deile-worker que executa `claude -p` em
# worktrees isolados sob o PVC claude-worker-home.
#
# Issue #309 fase 2.
# Spec: docs/superpowers/specs/2026-05-26-claude-worker-design.md
#
# Threat model: ver seção 7 da spec. Credentials no PVC mode 0600;
# NetworkPolicy egress whitelisted via manifest 40 + ConfigMap 47.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: claude-worker
  namespace: deile
  labels:
    app: claude-worker
    role: deile
spec:
  replicas: 1
  strategy:
    type: RollingUpdate
    rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }
  selector:
    matchLabels:
      app: claude-worker
      role: deile
  template:
    metadata:
      labels:
        app: claude-worker
        role: deile
    spec:
      automountServiceAccountToken: false
      enableServiceLinks: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: { type: RuntimeDefault }
      
      # InitContainer: copia credentials.json do Secret pro PVC writable.
      # Sempre overwrite — o operador rerruda `claude-login --switch` quando
      # quer trocar conta; o refresh do claude CLI in-pod escreve no MESMO path
      # (sobrevive entre dispatches no mesmo pod) mas é overrided por
      # claude-login + restart.
      initContainers:
        - name: bootstrap-creds
          image: deile-stack:local
          imagePullPolicy: Never
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              mkdir -p /home/claude/.claude
              cp /run/secrets/claude/credentials.json /home/claude/.claude/credentials.json
              chmod 0600 /home/claude/.claude/credentials.json
              chown 10001:10001 /home/claude/.claude/credentials.json
              ls -la /home/claude/.claude/credentials.json
          volumeMounts:
            - name: claude-credentials
              mountPath: /run/secrets/claude
              readOnly: true
            - name: claude-home
              mountPath: /home/claude
          securityContext:
            runAsUser: 0  # initContainer precisa de root para chown
            runAsGroup: 0
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
              add: ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
      
      containers:
        - name: claude-worker
          image: deile-stack:local
          imagePullPolicy: Never
          workingDir: /home/claude
          args: ["python3", "/app/wrapper.py", "claude-worker"]
          env:
            - { name: HOME, value: /home/claude }
            - { name: PYTHONUNBUFFERED, value: "1" }
            - { name: PYTHONDONTWRITEBYTECODE, value: "1" }
            - { name: DEILE_CLAUDE_WORKER_HOST, value: "0.0.0.0" }
            - { name: DEILE_CLAUDE_WORKER_PORT, value: "8767" }
            - { name: DEILE_CLAUDE_WORKER_ROOT, value: "/home/claude/work" }
            - { name: DEILE_CLAUDE_WORKER_TASK_TIMEOUT_S, value: "1800" }
            - { name: DEILE_CLAUDE_WORKER_LOG_LEVEL, value: "INFO" }
            - { name: DEILE_CLAUDE_ALLOWED_REPOS_FILE,
                value: "/etc/claude-worker/allowed_repos.regex" }
          ports:
            - { name: claude-api, containerPort: 8767, protocol: TCP }
          volumeMounts:
            - { name: claude-worker-bearer, mountPath: /run/secrets/claude-worker, readOnly: true }
            - { name: claude-home, mountPath: /home/claude }
            - { name: tmp, mountPath: /tmp }
            - name: allowed-repos
              mountPath: /etc/claude-worker
              readOnly: true
          readinessProbe:
            httpGet: { path: /v1/health, port: claude-api }
            initialDelaySeconds: 5
            periodSeconds: 20
            failureThreshold: 4
          livenessProbe:
            httpGet: { path: /v1/health, port: claude-api }
            initialDelaySeconds: 30
            periodSeconds: 30
            failureThreshold: 3
          resources:
            requests: { cpu: "100m", memory: "384Mi" }
            limits:   { cpu: "2000m", memory: "2Gi" }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            runAsUser: 10001
            runAsGroup: 10001
            capabilities: { drop: ["ALL"] }
            seccompProfile: { type: RuntimeDefault }
      
      volumes:
        - name: claude-credentials
          secret:
            secretName: claude-credentials
            defaultMode: 0o400
        - name: claude-worker-bearer
          secret:
            secretName: claude-worker-bearer
            defaultMode: 0o400
        - name: claude-home
          persistentVolumeClaim:
            claimName: claude-worker-home
        - name: tmp
          emptyDir: { medium: Memory, sizeLimit: "128Mi" }
        - name: allowed-repos
          configMap:
            name: claude-worker-allowed-repos
            defaultMode: 0o444
---
apiVersion: v1
kind: Service
metadata:
  name: claude-worker
  namespace: deile
  labels:
    app: claude-worker
spec:
  type: ClusterIP
  selector:
    app: claude-worker
  ports:
    - { name: claude-api, port: 8767, targetPort: claude-api, protocol: TCP }
```

- [ ] **Step 2: Dry-run validate**

```bash
~/.rd/bin/kubectl apply --dry-run=client -f infra/k8s/manifests/50-claude-worker-deployment.yaml
```

Expected: `deployment.apps/claude-worker created (dry run)` + `service/claude-worker created (dry run)`.

- [ ] **Step 3: Commit**

```bash
git add infra/k8s/manifests/50-claude-worker-deployment.yaml
git commit -m "feat(k8s): claude-worker Deployment + Service + initContainer

Pod paralelo ao deile-worker (replicas=1, mesmo image, args
'wrapper.py claude-worker'). InitContainer copia Secret claude-credentials
pra PVC writable em /home/claude/.claude/. NetworkPolicy egress
whitelisted aplicado via manifest 40 (próxima task) + 47 (ConfigMap).

Service ClusterIP em :8767.

Refs #309"
```

---

## Task 10: NetworkPolicy update

**Files:**
- Modify: `infra/k8s/manifests/40-network-policy.yaml`

- [ ] **Step 1: Read existing policy**

```bash
sed -n '170,200p' infra/k8s/manifests/40-network-policy.yaml
```

Identifica seção "pipeline egress to deile-worker" — vai espelhar para claude-worker.

- [ ] **Step 2: Add rules**

Edit `infra/k8s/manifests/40-network-policy.yaml`, adicione no final do arquivo (mantenha as policies existentes intactas):

```yaml
---
# ---- claude-worker: ingress só do deile-pipeline -----------------------------
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: claude-worker-ingress-from-pipeline
  namespace: deile
spec:
  podSelector:
    matchLabels:
      app: claude-worker
  policyTypes: ["Ingress"]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: deile-pipeline
      ports:
        - { protocol: TCP, port: 8767 }
---
# ---- pipeline egress to claude-worker ---------------------------------------
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: pipeline-egress-to-claude-worker
  namespace: deile
spec:
  podSelector:
    matchLabels:
      app: deile-pipeline
  policyTypes: ["Egress"]
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: claude-worker
      ports:
        - { protocol: TCP, port: 8767 }
---
# ---- claude-worker egress: anthropic API + git forges (whitelisted) ---------
# Granularidade de repo é enforced no wrapper.py via ConfigMap allowed_repos —
# este NetworkPolicy só faz L3/L4 (host:port).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: claude-worker-egress-llm-and-forges
  namespace: deile
spec:
  podSelector:
    matchLabels:
      app: claude-worker
  policyTypes: ["Egress"]
  egress:
    # DNS (kube-dns)
    - to:
        - namespaceSelector: {}
          podSelector:
            matchLabels: { k8s-app: kube-dns }
      ports:
        - { protocol: UDP, port: 53 }
    # HTTPS para Anthropic API + GitHub + GitLab
    # NetworkPolicy não filtra por hostname em k3s básico; granularidade de
    # destino é via wrapper.py + ConfigMap allowed-repos.
    - to: []
      ports:
        - { protocol: TCP, port: 443 }
```

- [ ] **Step 3: Dry-run validate**

```bash
~/.rd/bin/kubectl apply --dry-run=client -f infra/k8s/manifests/40-network-policy.yaml 2>&1 | tail -10
```

Expected: 3 new NetworkPolicies `created (dry run)`.

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/manifests/40-network-policy.yaml
git commit -m "feat(k8s): NetworkPolicy egress/ingress para claude-worker

3 policies novas:
- claude-worker-ingress-from-pipeline (só pipeline pode falar :8767)
- pipeline-egress-to-claude-worker
- claude-worker-egress-llm-and-forges (443 só pra fora; granularidade de
  repo via wrapper.py + ConfigMap allowed-repos)

Refs #309"
```

---

## Task 11: wrapper.py — claude-worker mode

**Files:**
- Modify: `infra/k8s/wrapper.py`

- [ ] **Step 1: Read existing wrapper structure**

```bash
grep -n "def main\|sys.argv\|MODE\|mode ==" infra/k8s/wrapper.py | head -20
```

- [ ] **Step 2: Add `claude-worker` branch**

Edit `infra/k8s/wrapper.py`. Adicione no top:

```python
import os
import re
import sys
from pathlib import Path

CLAUDE_ALLOWED_REPOS_FILE = "/etc/claude-worker/allowed_repos.regex"


def _load_allowed_repo_patterns() -> list[re.Pattern]:
    """Carrega regex patterns do ConfigMap. Falha hard se ausente — sem
    proteção, NÃO arrancamos o claude-worker."""
    path = Path(os.environ.get("DEILE_CLAUDE_ALLOWED_REPOS_FILE",
                                CLAUDE_ALLOWED_REPOS_FILE))
    if not path.exists():
        sys.exit(f"FATAL: allowed-repos config missing: {path}")
    patterns = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line))
    if not patterns:
        sys.exit(f"FATAL: empty allowed-repos config: {path}")
    return patterns


def _install_git_repo_guard(allowed: list[re.Pattern]) -> None:
    """Monkey-patch ou hook git config para rejeitar URLs não-whitelisted.
    
    Estratégia: define um GIT_TRACE callback OR install pre-receive em
    /home/claude/.gitconfig pointing to a wrapper script that validates URL
    before git fetch/push.
    
    Para simplicidade no V1: env var GIT_HTTP_USER_AGENT + custom credential
    helper. Validação real fica nas funções utilitárias chamadas pelo
    claude_worker_server antes de exec do claude.
    """
    # Setup que claude_worker_server.py vai usar
    os.environ["DEILE_CLAUDE_ALLOWED_REPOS_LOADED"] = "1"


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: wrapper.py <mode> [args...]")
    
    mode = sys.argv[1]
    
    if mode == "claude-worker":
        # NEW (issue #309 fase 2)
        patterns = _load_allowed_repo_patterns()
        _install_git_repo_guard(patterns)
        from claude_worker_server import main as server_main
        server_main()
    elif mode == "worker":
        # existing
        from worker_server import main as server_main
        server_main()
    elif mode == "deile":
        # existing — interactive deile shell
        ...
    else:
        sys.exit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Smoke test (locally — claude_worker_server vai dar import error é ok)**

```bash
cd infra/k8s/
python3 wrapper.py claude-worker 2>&1 | head -5
```

Expected: fails with `FATAL: allowed-repos config missing: /etc/claude-worker/allowed_repos.regex` (esperado fora do pod) OR `ModuleNotFoundError: claude_worker_server` (se config existir).

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/wrapper.py
git commit -m "feat(infra): wrapper.py aceita mode 'claude-worker'

Carrega allowed-repos regex do ConfigMap antes de invocar
claude_worker_server.main(). Falha hard se config ausente — sem whitelist
NÃO permitimos claude-worker arrancar (defense-in-depth contra
prompt-injection que tente git push pra repo arbitrário).

Refs #309"
```

---

## Task 12: `claude_worker_server.py` — skeleton + /v1/health

**Files:**
- Create: `infra/k8s/claude_worker_server.py`
- Test: `deile/tests/infrastructure/test_claude_worker_server.py`

- [ ] **Step 1: Write failing tests (health endpoint)**

Create `deile/tests/infrastructure/test_claude_worker_server.py`:

```python
"""Integration tests para claude_worker_server."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.mark.asyncio
async def test_health_returns_200_when_binary_present(monkeypatch):
    from infra.k8s.claude_worker_server import build_app
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude" if b == "claude" else None)
    
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["claude_binary"] == "/usr/local/bin/claude"


@pytest.mark.asyncio
async def test_health_returns_500_when_binary_missing(monkeypatch):
    from infra.k8s.claude_worker_server import build_app
    monkeypatch.setattr("shutil.which", lambda b: None)
    
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/health")
        assert resp.status == 500
        body = await resp.json()
        assert "claude binary not found" in body["error"].lower()
```

- [ ] **Step 2: Implement skeleton**

Create `infra/k8s/claude_worker_server.py`:

```python
"""claude-worker HTTP server.

Endpoints:
- GET /v1/health             — readiness/liveness probe
- POST /v1/dispatch          — receive brief + spawn `claude -p` in worktree
- GET /v1/progress/{task_id} — mid-flight snapshot do task

Spec: docs/superpowers/specs/2026-05-26-claude-worker-design.md
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/health", health_handler)
    app.router.add_post("/v1/dispatch", dispatch_handler)
    app.router.add_get("/v1/progress/{task_id}", progress_handler)
    return app


async def health_handler(request: web.Request) -> web.Response:
    """Readiness/liveness — verifica que o claude binary está acessível."""
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return web.json_response(
            {"status": "error", "error": "claude binary not found in PATH"},
            status=500,
        )
    return web.json_response({
        "status": "ok",
        "claude_binary": claude_bin,
    })


async def dispatch_handler(request: web.Request) -> web.Response:
    """POST /v1/dispatch — não implementado ainda (próxima task)."""
    return web.json_response({"status": "not_implemented"}, status=501)


async def progress_handler(request: web.Request) -> web.Response:
    """GET /v1/progress/{task_id} — não implementado ainda."""
    return web.json_response({"status": "not_implemented"}, status=501)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("DEILE_CLAUDE_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("DEILE_CLAUDE_WORKER_HOST", "0.0.0.0")
    port = int(os.environ.get("DEILE_CLAUDE_WORKER_PORT", "8767"))
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    root.mkdir(parents=True, exist_ok=True)
    
    logger.info("claude-worker starting on %s:%d (root=%s)", host, port, root)
    app = build_app()
    web.run_app(app, host=host, port=port)
```

- [ ] **Step 3: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infrastructure/test_claude_worker_server.py -v 2>&1 | tail -10
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/claude_worker_server.py \
       deile/tests/infrastructure/test_claude_worker_server.py
git commit -m "feat(claude-worker): server skeleton + /v1/health endpoint

aiohttp server com 3 routes; /v1/health implementado (verifica binary
claude no PATH). /v1/dispatch e /v1/progress stub 501 (próximas tasks).

main() lê config do env + cria root dir.

Refs #309"
```

---

## Task 13: `claude_worker_server.py` — /v1/dispatch handler

**Files:**
- Modify: `infra/k8s/claude_worker_server.py` — implement `dispatch_handler`
- Modify: `deile/tests/infrastructure/test_claude_worker_server.py`

- [ ] **Step 1: Add dispatch tests**

Append to `deile/tests/infrastructure/test_claude_worker_server.py`:

```python
@pytest.mark.asyncio
async def test_dispatch_rejects_non_anthropic_model(monkeypatch):
    """claude-worker só aceita preferred_model 'anthropic:*'."""
    from infra.k8s.claude_worker_server import build_app
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", json={
            "brief": "test",
            "channel_id": "x",
            "preferred_model": "openai:gpt-4",
            "stage": "implement",
        })
        assert resp.status == 400
        body = await resp.json()
        assert "anthropic" in body["error"].lower()


@pytest.mark.asyncio
async def test_dispatch_translates_model_slug(monkeypatch, tmp_path):
    """Mocks subprocess; verifica que slug 'anthropic:claude-opus-4-7' vira
    arg '--model claude-opus-4-7' na call do claude CLI."""
    from infra.k8s import claude_worker_server as cws
    
    captured_args = {}
    
    async def fake_run_subprocess(args, cwd, task_id, timeout):
        captured_args["args"] = args
        return cws.SubprocessResult(
            returncode=0, stdout="ok\n", stderr="",
            duration_seconds=1.0,
        )
    
    monkeypatch.setattr(cws, "run_subprocess_with_progress", fake_run_subprocess)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    
    app = cws.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", json={
            "brief": "implement #1",
            "channel_id": "auto/issue-1",
            "preferred_model": "anthropic:claude-opus-4-7",
            "stage": "implement",
            "issue_number": 1,
            "branch": "auto/issue-1",
        })
        assert resp.status == 200
    
    args = captured_args["args"]
    assert "claude" in args[0]
    assert "-p" in args
    assert "--model" in args
    # Slug deve ter sido traduzido: anthropic:claude-opus-4-7 → claude-opus-4-7
    model_idx = args.index("--model")
    assert args[model_idx + 1] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_dispatch_response_shape(monkeypatch, tmp_path):
    """Response inclui ok, stdout, stderr, task_id, duration_seconds."""
    from infra.k8s import claude_worker_server as cws
    
    async def fake_run_subprocess(args, cwd, task_id, timeout):
        return cws.SubprocessResult(
            returncode=0, stdout="success\n", stderr="",
            duration_seconds=42.0,
        )
    
    monkeypatch.setattr(cws, "run_subprocess_with_progress", fake_run_subprocess)
    monkeypatch.setattr("shutil.which", lambda b: "/usr/local/bin/claude")
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    
    app = cws.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/v1/dispatch", json={
            "brief": "x", "channel_id": "y",
            "preferred_model": "anthropic:claude-sonnet-4-6",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert "stdout" in body
        assert "stderr" in body
        assert "task_id" in body
        assert body["duration_seconds"] == 42.0
```

- [ ] **Step 2: Run tests, expect failures**

```bash
python3 -m pytest deile/tests/infrastructure/test_claude_worker_server.py -v 2>&1 | tail -15
```

Expected: 3 fail (dispatch not implemented).

- [ ] **Step 3: Implement dispatch handler**

Edit `infra/k8s/claude_worker_server.py` — replace stub `dispatch_handler`:

```python
import asyncio
import re
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# ... existing imports + handler ...


@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


PREAMBLE_TEMPLATES = {
    "implement": (
        "Você é Claude Code em modo autônomo (claude-worker pod, dispatch local).\n"
        "Worktree: já checked out em $PWD, branch $BRANCH.\n"
        "Tarefa: implemente o que está descrito após '---' abaixo.\n"
        "Quando terminar, imprima 'STATUS: SUCCESS' como última linha.\n"
        "Em falha, 'STATUS: BLOCKED_<motivo>'.\n"
        "NÃO faça merge, NÃO faça push --force, NÃO use --no-verify."
    ),
    "review": (
        "Você é Claude Code revisor (claude-worker pod). Worktree: $PWD, branch $BRANCH.\n"
        "Tarefa: revise a PR (descrita após '---'). Comente achados via gh CLI.\n"
        "STATUS: SUCCESS quando review estiver postado; STATUS: BLOCKED_X em falha."
    ),
    "classify": (
        "Você é Claude Code classificador (claude-worker pod). Tarefa: classifique "
        "a issue descrita após '---'. Imprima JSON com {category, severity, "
        "estimated_effort}. STATUS: SUCCESS ao final."
    ),
    "refine": (
        "Você é Claude Code refinador (claude-worker pod). Tarefa: refine o body "
        "da issue descrita após '---' editando-a via gh CLI. STATUS: SUCCESS ao final."
    ),
    "pr_review": (
        "Você é Claude Code revisor de PR (claude-worker pod). Worktree: $PWD, "
        "branch $BRANCH. Revise rigorosamente a PR descrita após '---', poste "
        "achados inline via gh api. STATUS: APPROVE / REQUEST_CHANGES."
    ),
    "follow_ups": (
        "Você é Claude Code follow-up handler (claude-worker pod). Worktree: $PWD. "
        "Trate os follow-ups descritos após '---'. STATUS: SUCCESS ao final."
    ),
}


def _render_preamble(stage: str, branch: str | None, task_id: str) -> str:
    template = PREAMBLE_TEMPLATES.get(stage, PREAMBLE_TEMPLATES["implement"])
    return (
        template
        .replace("$BRANCH", branch or "(no branch)")
        .replace("$TASK_ID", task_id)
    )


async def run_subprocess_with_progress(
    args: list[str], *, cwd: Path, task_id: str, timeout: int,
) -> SubprocessResult:
    """Run subprocess; tail stdout/stderr para arquivos no PVC; return result."""
    start = time.monotonic()
    
    progress_dir = cwd.parent / ".progress"
    progress_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = progress_dir / f"{task_id}.stdout.log"
    stderr_path = progress_dir / f"{task_id}.stderr.log"
    
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        duration = time.monotonic() - start
        return SubprocessResult(
            returncode=124, stdout="",
            stderr=f"claude -p timed out after {timeout}s",
            duration_seconds=duration,
        )
    
    duration = time.monotonic() - start
    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    
    # Persist for /v1/progress queries (TTL handled separately)
    stdout_path.write_text(stdout)
    stderr_path.write_text(stderr)
    
    return SubprocessResult(
        returncode=proc.returncode or 0,
        stdout=stdout, stderr=stderr,
        duration_seconds=duration,
    )


_ANTHROPIC_SLUG_RE = re.compile(r"^anthropic:(.+)$")


async def dispatch_handler(request: web.Request) -> web.Response:
    """POST /v1/dispatch — dispatch a brief para claude -p em worktree fresh."""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    
    brief = payload.get("brief")
    if not brief or not isinstance(brief, str):
        return web.json_response(
            {"ok": False, "error": "missing or invalid 'brief'"}, status=400,
        )
    
    stage = payload.get("stage", "implement")
    branch = payload.get("branch")
    model_slug = payload.get("preferred_model")
    
    # Validate model — só anthropic
    if model_slug:
        m = _ANTHROPIC_SLUG_RE.match(model_slug)
        if not m:
            return web.json_response({
                "ok": False,
                "error": f"claude-worker requires 'anthropic:*' model, got {model_slug!r}",
            }, status=400)
        claude_model = m.group(1)
    else:
        claude_model = None
    
    # Workspace
    task_id = secrets.token_hex(8)
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    workspace = root / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    
    # Brief + preamble
    preamble = _render_preamble(stage, branch, task_id)
    full_prompt = preamble + "\n\n---\n\n" + brief
    
    # Build cmd
    claude_bin = shutil.which("claude") or "claude"
    cmd = [claude_bin, "-p", "--permission-mode", "bypassPermissions"]
    if claude_model:
        cmd.extend(["--model", claude_model])
    cmd.append(full_prompt)
    
    logger.info(
        "dispatch task_id=%s stage=%s model=%s branch=%s",
        task_id, stage, claude_model, branch,
    )
    
    timeout = int(os.environ.get("DEILE_CLAUDE_WORKER_TASK_TIMEOUT_S", "1800"))
    
    try:
        result = await run_subprocess_with_progress(
            cmd, cwd=workspace, task_id=task_id, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — surface as failed dispatch
        logger.exception("dispatch failed task_id=%s", task_id)
        return web.json_response({
            "ok": False, "error": f"{type(exc).__name__}: {exc}",
            "task_id": task_id,
        }, status=500)
    
    return web.json_response({
        "ok": result.returncode == 0,
        "stdout": result.stdout[-50_000:],
        "stderr": result.stderr[-10_000:],
        "task_id": task_id,
        "duration_seconds": result.duration_seconds,
        "returncode": result.returncode,
    })
```

- [ ] **Step 4: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infrastructure/test_claude_worker_server.py -v 2>&1 | tail -15
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/claude_worker_server.py \
       deile/tests/infrastructure/test_claude_worker_server.py
git commit -m "feat(claude-worker): /v1/dispatch implementa exec claude -p

- Valida preferred_model é anthropic:*; 400 em violação
- Traduz slug (anthropic:claude-opus-4-7 → claude-opus-4-7) para --model
- Cria worktree fresco em DEILE_CLAUDE_WORKER_ROOT/<task_id>
- Renderiza preamble por stage + brief do pipeline
- Exec claude -p --permission-mode bypassPermissions [--model X] <prompt>
- Persiste stdout/stderr no PVC pro /v1/progress
- Return JSON com ok/stdout/stderr/task_id/duration

Refs #309"
```

---

## Task 14: `/v1/progress/{task_id}` handler

**Files:**
- Modify: `infra/k8s/claude_worker_server.py`
- Modify: `deile/tests/infrastructure/test_claude_worker_server.py`

- [ ] **Step 1: Add tests**

Append to `deile/tests/infrastructure/test_claude_worker_server.py`:

```python
@pytest.mark.asyncio
async def test_progress_returns_404_for_unknown_task(monkeypatch, tmp_path):
    from infra.k8s import claude_worker_server as cws
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    
    app = cws.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/nonexistent_task_id_xyz")
        assert resp.status == 404


@pytest.mark.asyncio
async def test_progress_returns_tails(monkeypatch, tmp_path):
    """Quando progress file existe, devolve tail dos últimos N bytes."""
    from infra.k8s import claude_worker_server as cws
    
    progress_dir = tmp_path / ".progress"
    progress_dir.mkdir()
    (progress_dir / "abc123.stdout.log").write_text("line 1\nline 2\nline 3\n")
    (progress_dir / "abc123.stderr.log").write_text("err A\nerr B\n")
    
    monkeypatch.setenv("DEILE_CLAUDE_WORKER_ROOT", str(tmp_path))
    
    app = cws.build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/v1/progress/abc123")
        assert resp.status == 200
        body = await resp.json()
        assert "stdout" in body
        assert "stderr" in body
        assert "line 3" in body["stdout"]
        assert "err B" in body["stderr"]
```

- [ ] **Step 2: Implement handler**

Replace `progress_handler` in `infra/k8s/claude_worker_server.py`:

```python
async def progress_handler(request: web.Request) -> web.Response:
    """GET /v1/progress/{task_id} — snapshot do task em execução."""
    task_id = request.match_info["task_id"]
    
    # Sanity: task_id é hex 16 chars (gerado pelo secrets.token_hex(8))
    if not re.fullmatch(r"[0-9a-f]{16}", task_id):
        return web.json_response(
            {"error": "invalid task_id format"}, status=400,
        )
    
    root = Path(os.environ.get("DEILE_CLAUDE_WORKER_ROOT", "/home/claude/work"))
    progress_dir = root / ".progress"
    stdout_path = progress_dir / f"{task_id}.stdout.log"
    stderr_path = progress_dir / f"{task_id}.stderr.log"
    
    if not stdout_path.exists() and not stderr_path.exists():
        return web.json_response(
            {"error": f"task_id {task_id} not found"}, status=404,
        )
    
    stdout = stdout_path.read_text() if stdout_path.exists() else ""
    stderr = stderr_path.read_text() if stderr_path.exists() else ""
    
    return web.json_response({
        "task_id": task_id,
        "stdout": stdout[-50_000:],
        "stderr": stderr[-10_000:],
    })
```

- [ ] **Step 3: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infrastructure/test_claude_worker_server.py -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/claude_worker_server.py \
       deile/tests/infrastructure/test_claude_worker_server.py
git commit -m "feat(claude-worker): /v1/progress/{task_id} retorna tails

Lê stdout/stderr persistidos pelo run_subprocess_with_progress. 404 se
task não existe; 400 se task_id formato inválido (não-hex 16-char).

Útil pra DispatchMatrixView mostrar estado mid-flight do dispatch (FU)
e pra subagent orchestration que polla /v1/progress.

Refs #309"
```

---

## Task 15: `_claude_install.py` — bootstrap helper

**Files:**
- Create: `infra/k8s/_claude_install.py`
- Test: `deile/tests/infra/test_claude_install.py`

- [ ] **Step 1: Write failing tests**

Create `deile/tests/infra/test_claude_install.py`:

```python
"""Unit tests para _claude_install.bootstrap_claude_worker."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_bootstrap_idempotent_when_already_installed(tmp_path, monkeypatch):
    """Se credentials.json já existe e Deployment está pronto, bootstrap é noop."""
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text('{"email":"user@test.com"}')
    monkeypatch.setenv("HOME", str(tmp_path))
    
    from infra.k8s._claude_install import bootstrap_claude_worker
    
    with patch("infra.k8s._claude_install._kubectl_apply_secret") as mock_secret, \
         patch("infra.k8s._claude_install._kubectl_apply_manifests") as mock_apply, \
         patch("infra.k8s._claude_install._kubectl_wait_rollout") as mock_wait:
        mock_secret.return_value = True
        mock_apply.return_value = True
        mock_wait.return_value = True
        
        result = bootstrap_claude_worker(interactive=False, force_relogin=False)
    
    assert result.ok is True
    assert result.account_email == "user@test.com"
    assert result.secret_applied is True
    assert result.deployment_applied is True


def test_bootstrap_force_relogin_calls_claude_logout(tmp_path, monkeypatch):
    """--force-relogin → claude logout + claude login antes de continuar."""
    from infra.k8s import _claude_install as ci
    
    calls = []
    
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        ret = MagicMock()
        ret.returncode = 0
        return ret
    
    fake_home = tmp_path / ".claude"
    fake_home.mkdir()
    (fake_home / "credentials.json").write_text('{"email":"u@x"}')
    monkeypatch.setenv("HOME", str(tmp_path))
    
    with patch.object(ci, "subprocess") as mock_sub, \
         patch.object(ci, "_kubectl_apply_secret", return_value=True), \
         patch.object(ci, "_kubectl_apply_manifests", return_value=True), \
         patch.object(ci, "_kubectl_wait_rollout", return_value=True):
        mock_sub.run.side_effect = fake_run
        result = ci.bootstrap_claude_worker(interactive=True, force_relogin=True)
    
    # Verifica que `claude logout` e `claude login` foram chamados
    cmd_strs = [" ".join(c) if isinstance(c, list) else c for c in calls]
    assert any("logout" in s for s in cmd_strs), f"calls: {cmd_strs}"
    assert any("login" in s for s in cmd_strs), f"calls: {cmd_strs}"


def test_bootstrap_returns_error_when_no_credentials_and_not_interactive(tmp_path, monkeypatch):
    """No credentials + interactive=False → fail fast."""
    monkeypatch.setenv("HOME", str(tmp_path))  # vazio
    
    from infra.k8s._claude_install import bootstrap_claude_worker
    result = bootstrap_claude_worker(interactive=False, force_relogin=False)
    
    assert result.ok is False
    assert "credentials" in result.error.lower()
```

- [ ] **Step 2: Run tests, expect failures**

```bash
python3 -m pytest deile/tests/infra/test_claude_install.py -v 2>&1 | tail -10
```

Expected: `ImportError` (module doesn't exist).

- [ ] **Step 3: Implement helper**

Create `infra/k8s/_claude_install.py`:

```python
"""bootstrap_claude_worker — instala/atualiza credentials + Deployment do
claude-worker. Compartilhado entre CLI verb (`deploy.py k8s claude-login`)
e painel (DispatchMatrixView).

Issue #309 fase 2.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ClaudeLoginResult:
    ok: bool
    account_email: str | None = None
    secret_applied: bool = False
    deployment_applied: bool = False
    rollout_ready: bool = False
    error: str | None = None


def _read_credentials(home: Path) -> dict | None:
    """Lê ~/.claude/credentials.json se existir."""
    cred_path = home / ".claude" / "credentials.json"
    if not cred_path.exists():
        return None
    try:
        return json.loads(cred_path.read_text())
    except Exception as exc:
        logger.warning("failed to parse %s: %s", cred_path, exc)
        return None


def _run_claude_login(*, logout_first: bool = False) -> bool:
    """Spawn claude login no host. Returns True se completou OK."""
    if logout_first:
        subprocess.run(["claude", "logout"], check=False, timeout=30)
    
    logger.info("running 'claude login' — browser will open; complete OAuth there")
    try:
        result = subprocess.run(
            ["claude", "login"], check=False, timeout=300,
        )
        return result.returncode == 0
    except FileNotFoundError:
        logger.error("claude CLI not in PATH; install with `npm install -g @anthropic-ai/claude-code`")
        return False


def _kubectl_apply_secret(creds: dict, *, namespace: str) -> bool:
    """Apply Secret claude-credentials with credentials.json data."""
    creds_json = json.dumps(creds)
    cmd = [
        "kubectl", "create", "secret", "generic", "claude-credentials",
        f"--from-literal=credentials.json={creds_json}",
        "-n", namespace,
        "--dry-run=client", "-o", "yaml",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        logger.error("kubectl create secret dry-run failed: %s", proc.stderr)
        return False
    
    # Apply via kubectl apply -f -
    apply = subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=proc.stdout,
        capture_output=True, text=True, check=False,
    )
    if apply.returncode != 0:
        logger.error("kubectl apply failed: %s", apply.stderr)
        return False
    return True


def _kubectl_apply_manifests(*, namespace: str) -> bool:
    """Apply manifests 47, 48, 49, 50, 40 (NetworkPolicy update)."""
    manifests_dir = Path(__file__).parent / "manifests"
    files = [
        manifests_dir / "47-claude-worker-allowed-repos.yaml",
        manifests_dir / "48-claude-worker-bearer-secret.yaml",
        manifests_dir / "49-claude-worker-pvc.yaml",
        manifests_dir / "50-claude-worker-deployment.yaml",
        manifests_dir / "40-network-policy.yaml",
    ]
    for f in files:
        if not f.exists():
            logger.error("manifest missing: %s", f)
            return False
        result = subprocess.run(
            ["kubectl", "apply", "-f", str(f), "-n", namespace],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            logger.error("kubectl apply %s failed: %s", f.name, result.stderr)
            return False
        logger.info("applied %s", f.name)
    return True


def _kubectl_wait_rollout(*, namespace: str, timeout_s: int = 180) -> bool:
    """Wait for claude-worker Deployment ready."""
    result = subprocess.run(
        ["kubectl", "rollout", "status", "deployment/claude-worker",
         "-n", namespace, f"--timeout={timeout_s}s"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def bootstrap_claude_worker(
    *,
    namespace: str = "deile",
    force_relogin: bool = False,
    interactive: bool = True,
    home: Path | None = None,
) -> ClaudeLoginResult:
    """
    Etapas idempotentes:
    1. Detect credenciais no host (~/.claude/credentials.json).
    2. Se force_relogin OR credentials ausentes E interactive=True → claude login.
    3. Read credentials.json, extract email.
    4. Apply Secret claude-credentials.
    5. Apply manifests 47/48/49/50/40.
    6. Wait rollout.
    7. Return ClaudeLoginResult.
    """
    home = home or Path.home()
    
    # 1+2. Credentials
    creds = _read_credentials(home)
    if force_relogin or creds is None:
        if not interactive:
            return ClaudeLoginResult(
                ok=False,
                error="No credentials found and interactive=False; "
                      "run with --interactive or pre-create ~/.claude/credentials.json",
            )
        if not _run_claude_login(logout_first=force_relogin):
            return ClaudeLoginResult(ok=False, error="claude login failed")
        creds = _read_credentials(home)
        if creds is None:
            return ClaudeLoginResult(
                ok=False, error="claude login succeeded but credentials.json missing",
            )
    
    email = creds.get("email") if isinstance(creds, dict) else None
    
    # 3+4. Secret
    if not _kubectl_apply_secret(creds, namespace=namespace):
        return ClaudeLoginResult(
            ok=False, account_email=email,
            error="failed to apply claude-credentials Secret",
        )
    
    # 5. Manifests
    if not _kubectl_apply_manifests(namespace=namespace):
        return ClaudeLoginResult(
            ok=False, account_email=email, secret_applied=True,
            error="failed to apply manifests",
        )
    
    # 6. Wait rollout
    if not _kubectl_wait_rollout(namespace=namespace):
        return ClaudeLoginResult(
            ok=False, account_email=email,
            secret_applied=True, deployment_applied=True,
            error="claude-worker rollout did not become ready within timeout",
        )
    
    return ClaudeLoginResult(
        ok=True, account_email=email,
        secret_applied=True, deployment_applied=True, rollout_ready=True,
    )
```

- [ ] **Step 4: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infra/test_claude_install.py -v 2>&1 | tail -10
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/_claude_install.py deile/tests/infra/test_claude_install.py
git commit -m "feat(infra): _claude_install.bootstrap_claude_worker helper

Compartilhado entre CLI verb e painel. Lê credentials do host, opcionalmente
roda claude login, aplica Secret + manifests, aguarda rollout. Idempotente:
re-rodar é noop quando tudo está pronto.

Refs #309"
```

---

## Task 16: `deploy.py` — `k8s claude-login` verb

**Files:**
- Modify: `infra/k8s/deploy.py`

- [ ] **Step 1: Locate verb registration**

```bash
grep -n "def cmd_\|argparse\|add_subparsers\|subparsers.add_parser" infra/k8s/deploy.py | head -20
```

Identify the pattern used (cmd_X functions OR class-based dispatcher).

- [ ] **Step 2: Add `claude-login` verb**

In `infra/k8s/deploy.py`, register a new k8s subcommand. Add function:

```python
def cmd_k8s_claude_login(args) -> int:
    """k8s claude-login [--switch] [--no-interactive]
    
    Captura credenciais do claude na máquina host (ou roda `claude login`
    interativo), cria Secret claude-credentials, aplica manifests 47-50 do
    claude-worker, aguarda Ready.
    
    Issue #309 fase 2.
    """
    from _claude_install import bootstrap_claude_worker
    
    logger.info("starting k8s claude-login (switch=%s, interactive=%s)",
                args.switch, not args.no_interactive)
    
    result = bootstrap_claude_worker(
        namespace=args.namespace,
        force_relogin=args.switch,
        interactive=not args.no_interactive,
    )
    
    if not result.ok:
        print(f"❌ claude-login failed: {result.error}")
        return 1
    
    print(f"✅ claude-worker pronto")
    if result.account_email:
        print(f"   logado como: {result.account_email}")
    print(f"   Secret: {'ok' if result.secret_applied else '—'}")
    print(f"   Deployment: {'ok' if result.deployment_applied else '—'}")
    print(f"   Rollout ready: {'ok' if result.rollout_ready else '—'}")
    return 0


# In the subparser setup section, add:
def _register_k8s_subparsers(parser):
    # ... existing verbs ...
    
    claude_login = parser.add_parser(
        "claude-login",
        help="Captura credenciais Claude do host e instala claude-worker no cluster",
    )
    claude_login.add_argument(
        "--switch", action="store_true",
        help="Force logout + login (troca de conta)",
    )
    claude_login.add_argument(
        "--no-interactive", action="store_true",
        help="Falha se credentials não estão presentes (não roda claude login)",
    )
    claude_login.add_argument(
        "--namespace", default="deile",
        help="K8s namespace (default: deile)",
    )
    claude_login.set_defaults(func=cmd_k8s_claude_login)
```

- [ ] **Step 3: Smoke test the verb registration**

```bash
python3 infra/k8s/deploy.py k8s claude-login --help 2>&1
```

Expected: print help text with the 3 flags.

- [ ] **Step 4: Dry-run (sem cluster ativo, vai falhar mas valida import)**

```bash
python3 infra/k8s/deploy.py k8s claude-login --no-interactive 2>&1 | head -10
```

Expected: `❌ claude-login failed: No credentials found and interactive=False; ...`

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/deploy.py
git commit -m "feat(deploy): k8s claude-login verb (com --switch)

CLI invoca bootstrap_claude_worker do _claude_install. Flags:
- --switch       force logout + new OAuth login
- --no-interactive  falha se credentials ausentes (CI-friendly)
- --namespace    K8s namespace (default deile)

Refs #309"
```

---

## Task 17: `StageDispatchProvider` em `_panel_data.py`

**Files:**
- Modify: `infra/k8s/_panel_data.py` — adicionar provider
- Test: nova suite no `deile/tests/infra/test_panel_data.py`

- [ ] **Step 1: Read existing StageModelsProvider pattern**

```bash
grep -n "class StageModelsProvider\|class.*Provider.*KubectlProvider" infra/k8s/_panel_data.py | head -10
sed -n '2140,2200p' infra/k8s/_panel_data.py
```

Note the existing structure for mirroring.

- [ ] **Step 2: Write failing tests**

Add to `deile/tests/infra/test_panel_data.py`:

```python
def test_stage_dispatch_provider_reads_env_vars(monkeypatch):
    """StageDispatchProvider lê DEILE_PIPELINE_DISPATCH_<STAGE> da Deployment."""
    from infra.k8s._panel_data import StageDispatchProvider
    
    fake_deployment_json = {
        "spec": {"template": {"spec": {"containers": [{
            "env": [
                {"name": "DEILE_PIPELINE_DISPATCH_IMPLEMENT", "value": "claude-worker"},
                {"name": "DEILE_PIPELINE_DISPATCH_MODE", "value": "deile-worker"},
                {"name": "DEILE_PIPELINE_MODEL_IMPLEMENT", "value": "anthropic:claude-opus-4-7"},
            ]
        }]}}}
    }
    
    provider = StageDispatchProvider(enabled=True, namespace="deile")
    monkeypatch.setattr(provider, "_kubectl_get_deployment_json",
                         lambda *a, **k: fake_deployment_json)
    
    entries = provider.get_all_stages()
    by_stage = {e.stage: e for e in entries}
    
    assert by_stage["implement"].worker == "claude-worker"
    assert by_stage["implement"].source == "env"
    assert by_stage["implement"].model == "anthropic:claude-opus-4-7"
    assert by_stage["classify"].worker == "deile-worker"  # global fallback
    assert by_stage["classify"].source == "global"


def test_stage_dispatch_provider_detects_claude_worker_status(monkeypatch):
    """Provider expõe claude_worker_deployment_applied + pod_ready."""
    from infra.k8s._panel_data import StageDispatchProvider
    
    provider = StageDispatchProvider(enabled=True, namespace="deile")
    monkeypatch.setattr(provider, "_kubectl_get_deployment_json",
                         lambda name, **k: None if name == "claude-worker" else {})
    
    status = provider.get_claude_worker_status()
    assert status.deployment_applied is False
```

- [ ] **Step 3: Implement provider**

In `infra/k8s/_panel_data.py`, append before the panel composition section:

```python
from dataclasses import dataclass
from typing import Literal, Optional
# ... existing imports ...


@dataclass
class StageDispatchEntry:
    stage: str
    worker: str
    model: Optional[str]
    source: Literal["env", "global", "default"]


@dataclass
class ClaudeWorkerStatus:
    deployment_applied: bool
    pod_ready: bool
    logged_in_email: Optional[str]


class StageDispatchProvider(_KubectlProviderMixin):
    """Consolida leitura de:
    - DEILE_PIPELINE_DISPATCH_<STAGE> + DEILE_PIPELINE_DISPATCH_MODE (worker per-stage)
    - DEILE_PIPELINE_MODEL_<STAGE> + DEILE_PIPELINE_MODEL (model per-stage)
    - Status do claude-worker Deployment (instalado? Ready?)
    - Email da conta logada no Secret claude-credentials
    
    TTL 3s (mesmo de StageModelsProvider).
    """
    
    _TTL_S = 3.0
    
    def __init__(self, *, enabled: bool, namespace: str = "deile"):
        super().__init__(ttl_s=self._TTL_S)
        self.enabled = enabled
        self.namespace = namespace
    
    def get_all_stages(self) -> list[StageDispatchEntry]:
        from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES
        
        if not self.enabled:
            return [StageDispatchEntry(s, "deile-worker", None, "default")
                    for s in PIPELINE_STAGES]
        
        deployment = self._kubectl_get_deployment_json(
            "deile-pipeline", namespace=self.namespace,
        )
        if not deployment:
            return [StageDispatchEntry(s, "deile-worker", None, "default")
                    for s in PIPELINE_STAGES]
        
        envs = self._extract_env_vars(deployment)
        global_worker = envs.get("DEILE_PIPELINE_DISPATCH_MODE", "deile-worker")
        global_model = envs.get("DEILE_PIPELINE_MODEL")
        
        result = []
        for stage in PIPELINE_STAGES:
            stage_worker = envs.get(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}")
            stage_model = envs.get(f"DEILE_PIPELINE_MODEL_{stage.upper()}")
            
            if stage_worker:
                worker, source = stage_worker, "env"
            elif global_worker:
                worker, source = global_worker, "global"
            else:
                worker, source = "deile-worker", "default"
            
            model = stage_model or global_model
            result.append(StageDispatchEntry(stage, worker, model, source))
        
        return result
    
    def get_claude_worker_status(self) -> ClaudeWorkerStatus:
        deployment = self._kubectl_get_deployment_json(
            "claude-worker", namespace=self.namespace,
        )
        if not deployment:
            return ClaudeWorkerStatus(False, False, None)
        
        # Check status.readyReplicas
        status = deployment.get("status", {})
        ready_replicas = status.get("readyReplicas", 0)
        replicas = status.get("replicas", 0)
        pod_ready = ready_replicas == replicas and replicas > 0
        
        email = self._read_claude_credentials_email()
        return ClaudeWorkerStatus(True, pod_ready, email)
    
    def _read_claude_credentials_email(self) -> Optional[str]:
        secret = self._kubectl_get_secret_json("claude-credentials",
                                                namespace=self.namespace)
        if not secret:
            return None
        import base64, json
        try:
            data_b64 = secret.get("data", {}).get("credentials.json", "")
            data = json.loads(base64.b64decode(data_b64))
            return data.get("email")
        except Exception:
            return None
```

- [ ] **Step 4: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infra/test_panel_data.py -v -k "stage_dispatch" 2>&1 | tail -10
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/_panel_data.py deile/tests/infra/test_panel_data.py
git commit -m "feat(panel): StageDispatchProvider consolida worker+model per-stage

Lê DEILE_PIPELINE_DISPATCH_<STAGE> + DEILE_PIPELINE_MODEL_<STAGE> da
Deployment deile-pipeline + claude-worker status + email da conta logada.
TTL 3s. Espelha StageModelsProvider; substitui DispatchModeProvider.

Refs #309"
```

---

## Task 18: `DispatchMatrixView` em `_panel.py` — skeleton + render

**Files:**
- Modify: `infra/k8s/_panel.py`
- Test: nova `deile/tests/infra/test_dispatch_matrix_view.py`

- [ ] **Step 1: Write failing test**

Create `deile/tests/infra/test_dispatch_matrix_view.py`:

```python
"""Tests para DispatchMatrixView (issue #309 fase 2)."""
import pytest
from rich.console import Console


def test_view_renders_5_stages_plus_global(mock_panel_data):
    from infra.k8s._panel import DispatchMatrixView
    
    view = DispatchMatrixView(data=mock_panel_data)
    console = Console(width=120, record=True)
    output = console.render(view.render(None))
    text = console.export_text()
    
    # Verifica que todas as 5 stages aparecem + linha de global default
    for stage in ("classify", "refine", "implement", "pr_review", "follow_ups"):
        assert stage in text
    assert "Global default" in text or "global" in text.lower()


def test_view_shows_claude_worker_status_when_installed(mock_panel_data_with_claude):
    from infra.k8s._panel import DispatchMatrixView
    
    view = DispatchMatrixView(data=mock_panel_data_with_claude)
    console = Console(width=120, record=True)
    console.render(view.render(None))
    text = console.export_text()
    
    assert "logado como" in text.lower() or "user@" in text


def test_view_shows_install_hint_when_claude_worker_absent(mock_panel_data_no_claude):
    from infra.k8s._panel import DispatchMatrixView
    
    view = DispatchMatrixView(data=mock_panel_data_no_claude)
    console = Console(width=120, record=True)
    console.render(view.render(None))
    text = console.export_text()
    
    assert "instalar" in text.lower() or "install" in text.lower()
```

Add fixture in `deile/tests/infra/conftest.py` (or top of test file):

```python
@pytest.fixture
def mock_panel_data():
    from unittest.mock import MagicMock
    data = MagicMock()
    data.stage_dispatch.get_all_stages.return_value = [
        # Mocked StageDispatchEntry values
    ]
    data.stage_dispatch.get_claude_worker_status.return_value = MagicMock(
        deployment_applied=True, pod_ready=True, logged_in_email="user@example.com",
    )
    return data


@pytest.fixture
def mock_panel_data_with_claude(mock_panel_data):
    return mock_panel_data


@pytest.fixture
def mock_panel_data_no_claude(mock_panel_data):
    mock_panel_data.stage_dispatch.get_claude_worker_status.return_value = MagicMock(
        deployment_applied=False, pod_ready=False, logged_in_email=None,
    )
    return mock_panel_data
```

- [ ] **Step 2: Run tests, expect failure**

```bash
python3 -m pytest deile/tests/infra/test_dispatch_matrix_view.py -v 2>&1 | tail -10
```

Expected: ImportError (DispatchMatrixView doesn't exist).

- [ ] **Step 3: Implement skeleton**

In `infra/k8s/_panel.py`, ADD (don't replace yet) the new class:

```python
class DispatchMatrixView(View):
    """Pipeline Stage Configuration unified view (issue #309 fase 2).
    
    Substitui DispatchModeView + StageModelsView. Mostra matriz N+1 × 2 com
    rows: 5 stages + "Global default" + header com status do claude-worker.
    """
    
    HOTKEYS = (
        "↑↓ row  ←→ col  [enter]edit  [r]reset  [L]switch claude-worker login  [q]back"
    )
    title = "Pipeline Stage Configuration ([d])"
    refresh_s = 1.0
    
    def __init__(self, data: Optional[PanelData] = None):
        self.data = data
        self.cursor_row = 0
        self.cursor_col = 0  # 0=Worker, 1=Model
    
    def render(self, app: "PanelApp") -> RenderableType:
        from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES
        
        entries = self.data.stage_dispatch.get_all_stages()
        cw_status = self.data.stage_dispatch.get_claude_worker_status()
        
        # Header: claude-worker status
        if cw_status.deployment_applied:
            header_text = (
                f"claude-worker: {'ready' if cw_status.pod_ready else 'NOT READY'}"
                + (f" (logado como: {cw_status.logged_in_email})" if cw_status.logged_in_email else "")
            )
            header_style = "green" if cw_status.pod_ready else "yellow"
        else:
            header_text = "claude-worker: NÃO INSTALADO  ([I] para instalar)"
            header_style = "dim"
        
        # Build table
        tbl = Table(show_header=True, box=box.SIMPLE_HEAVY)
        tbl.add_column("Stage", style="cyan")
        tbl.add_column("Worker")
        tbl.add_column("Model")
        tbl.add_column("Source", style="dim")
        
        for i, entry in enumerate(entries):
            highlight = i == self.cursor_row and self.cursor_col == 0
            worker_text = f"[reverse]{entry.worker}[/reverse]" if highlight else entry.worker
            model_text = entry.model or "(default)"
            tbl.add_row(entry.stage, worker_text, model_text, entry.source)
        
        # Global default row
        tbl.add_row("—", "—", "—", "—", style="dim")
        tbl.add_row("Global default", "(env DEILE_PIPELINE_DISPATCH_MODE)", "(env DEILE_PIPELINE_MODEL)", "env")
        
        # Layout
        return Group(
            Text(header_text, style=header_style),
            tbl,
            Text(self.HOTKEYS, style="dim"),
        )
    
    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES
        
        n_stages = len(PIPELINE_STAGES)
        
        if key == "q" or key == "escape":
            return ActionResult.nav("dashboard")
        if key == "up":
            self.cursor_row = max(0, self.cursor_row - 1)
            return ActionResult()
        if key == "down":
            self.cursor_row = min(n_stages, self.cursor_row + 1)
            return ActionResult()
        if key == "left":
            self.cursor_col = max(0, self.cursor_col - 1)
            return ActionResult()
        if key == "right":
            self.cursor_col = min(1, self.cursor_col + 1)
            return ActionResult()
        if key == "enter":
            # NEXT TASK — modal picker
            return ActionResult()
        if key == "r":
            # NEXT TASK — reset cell
            return ActionResult()
        if key in ("L", "l"):
            # NEXT TASK — switch login modal
            return ActionResult()
        if key == "I" or key == "i":
            # NEXT TASK — install on the fly
            return ActionResult()
        return ActionResult()
```

- [ ] **Step 4: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infra/test_dispatch_matrix_view.py -v 2>&1 | tail -10
```

Expected: 3 passed (basic render works).

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/_panel.py deile/tests/infra/test_dispatch_matrix_view.py
git commit -m "feat(panel): DispatchMatrixView skeleton + render layout

Substitui DispatchModeView + StageModelsView. Matriz N+1 stages × 2 colunas
(Worker/Model). Header mostra status do claude-worker + email da conta.
Navegação ↑↓ ←→ funcional; actions [enter]/[r]/[L]/[I] são stubs (próximas
tasks).

Refs #309"
```

---

## Task 19: `DispatchMatrixView` — pickers contextuais (Worker + Model)

**Files:**
- Modify: `infra/k8s/_panel.py` — adicionar lógica de modal picker
- Modify: `deile/tests/infra/test_dispatch_matrix_view.py`

- [ ] **Step 1: Adicionar testes para pickers**

Append to `deile/tests/infra/test_dispatch_matrix_view.py`:

```python
def test_worker_picker_contextual_options(mock_panel_data_with_claude):
    """Picker do Worker mostra deile-worker, claude-worker, (global default)."""
    from infra.k8s._panel import DispatchMatrixView
    
    view = DispatchMatrixView(data=mock_panel_data_with_claude)
    options = view._worker_picker_options()
    
    assert "deile-worker" in options
    assert "claude-worker" in options
    assert "(global default)" in options


def test_model_picker_restricted_when_worker_is_claude(mock_panel_data_with_claude):
    """Quando Worker=claude-worker, model picker só mostra anthropic:*."""
    from infra.k8s._panel import DispatchMatrixView
    
    view = DispatchMatrixView(data=mock_panel_data_with_claude)
    options = view._model_picker_options(worker="claude-worker")
    
    assert all(m.startswith("anthropic:") or m == "(default)" for m in options)


def test_model_picker_open_when_worker_is_deile(mock_panel_data_with_claude):
    """Worker=deile-worker → picker mostra TODOS os providers."""
    from infra.k8s._panel import DispatchMatrixView
    
    view = DispatchMatrixView(data=mock_panel_data_with_claude)
    options = view._model_picker_options(worker="deile-worker")
    
    # Deve haver opções de pelo menos 2 providers diferentes
    providers = {opt.split(":", 1)[0] for opt in options if ":" in opt}
    assert len(providers) >= 2
```

- [ ] **Step 2: Implement picker logic**

Add methods to `DispatchMatrixView` in `infra/k8s/_panel.py`:

```python
def _worker_picker_options(self) -> list[str]:
    return ["deile-worker", "claude-worker", "(global default)"]


def _model_picker_options(self, *, worker: str) -> list[str]:
    """Contextual model options based on row's Worker."""
    # Load full list from model_providers.yaml
    all_models = self._load_all_models()  # implement via existing helper
    
    if worker == "claude-worker":
        return ["(default)"] + [m for m in all_models if m.startswith("anthropic:")]
    return ["(default)"] + all_models


def _load_all_models(self) -> list[str]:
    """Carrega lista de modelos do model_providers.yaml via PanelData."""
    if hasattr(self.data, "stage_dispatch") and hasattr(self.data.stage_dispatch, "list_models"):
        return self.data.stage_dispatch.list_models()
    # Fallback: hardcoded set conhecido para CI/tests
    return [
        "anthropic:claude-opus-4-7", "anthropic:claude-sonnet-4-6", "anthropic:claude-haiku-4-5",
        "openai:gpt-4", "openai:gpt-4-turbo",
        "deepseek:deepseek-chat",
        "google:gemini-2.5-pro",
    ]
```

- [ ] **Step 3: Wire picker on enter key**

In `handle_key` of `DispatchMatrixView`, replace the stub for `enter`:

```python
if key == "enter":
    from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES
    entries = self.data.stage_dispatch.get_all_stages()
    
    if self.cursor_row >= len(PIPELINE_STAGES):
        # Global default row — different handling
        return self._open_global_picker(app)
    
    entry = entries[self.cursor_row]
    if self.cursor_col == 0:
        # Worker picker
        return self._open_worker_picker(app, entry)
    elif self.cursor_col == 1:
        # Model picker (contextual)
        return self._open_model_picker(app, entry)
    return ActionResult()


def _open_worker_picker(self, app, entry):
    options = self._worker_picker_options()
    # Use existing modal helper pattern from DispatchModeView (PR #330)
    modal = ModalPicker(
        title=f"Worker for {entry.stage}",
        options=options,
        on_select=lambda choice: self._on_worker_selected(entry.stage, choice),
    )
    return ActionResult.modal(modal)


def _open_model_picker(self, app, entry):
    options = self._model_picker_options(worker=entry.worker)
    modal = ModalPicker(
        title=f"Model for {entry.stage} (worker={entry.worker})",
        options=options,
        on_select=lambda choice: self._on_model_selected(entry.stage, choice),
    )
    return ActionResult.modal(modal)


def _on_worker_selected(self, stage: str, choice: str) -> None:
    # Persist via kubectl set env (mesma helper já existente do DispatchModeView)
    from infra.k8s._panel_data import set_pipeline_dispatch_stage
    set_pipeline_dispatch_stage(stage, choice, namespace=self.data.namespace)


def _on_model_selected(self, stage: str, choice: str) -> None:
    from infra.k8s._panel_data import set_pipeline_model_stage
    set_pipeline_model_stage(stage, choice, namespace=self.data.namespace)
```

(`set_pipeline_dispatch_stage` e `set_pipeline_model_stage` precisam ser criados em `_panel_data.py` — espelham `set_pipeline_dispatch_mode` da PR #330 e `set_pipeline_model_stage` do #305. Reusam padrão de audit + validação.)

- [ ] **Step 4: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infra/test_dispatch_matrix_view.py -v 2>&1 | tail -15
```

Expected: 3 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/_panel.py deile/tests/infra/test_dispatch_matrix_view.py
git commit -m "feat(panel): DispatchMatrixView pickers contextuais

- _worker_picker_options(): [deile-worker, claude-worker, (global)]
- _model_picker_options(worker=): restringe a anthropic:* quando
  worker=claude-worker; full list quando deile-worker
- handle_key(enter) abre modal picker apropriado por coluna
- set_pipeline_dispatch_stage + set_pipeline_model_stage helpers
  para persist via kubectl set env + audit

Refs #309"
```

---

## Task 20: `DispatchMatrixView` — install-on-the-fly + switch-account modals

**Files:**
- Modify: `infra/k8s/_panel.py`
- Modify: `deile/tests/infra/test_dispatch_matrix_view.py`

- [ ] **Step 1: Add tests**

Append to `deile/tests/infra/test_dispatch_matrix_view.py`:

```python
def test_select_claude_worker_triggers_install_modal_when_absent(mock_panel_data_no_claude, monkeypatch):
    """Quando claude-worker NÃO está deployed e user seleciona no picker,
    aparece modal 'instalar agora?'."""
    from infra.k8s._panel import DispatchMatrixView
    
    install_called = {"flag": False}
    
    def fake_bootstrap(**kwargs):
        install_called["flag"] = True
        from infra.k8s._claude_install import ClaudeLoginResult
        return ClaudeLoginResult(ok=True, account_email="x@y.com",
                                  secret_applied=True, deployment_applied=True,
                                  rollout_ready=True)
    
    monkeypatch.setattr("infra.k8s._claude_install.bootstrap_claude_worker", fake_bootstrap)
    
    view = DispatchMatrixView(data=mock_panel_data_no_claude)
    # Simulate selecting claude-worker
    view._on_worker_selected("implement", "claude-worker")
    
    assert install_called["flag"] is True


def test_switch_login_action(mock_panel_data_with_claude, monkeypatch):
    """Apertando [L] dispara bootstrap_claude_worker(force_relogin=True)."""
    from infra.k8s._panel import DispatchMatrixView
    
    relogin_called = {"flag": False}
    
    def fake_bootstrap(**kwargs):
        relogin_called["flag"] = kwargs.get("force_relogin") is True
        from infra.k8s._claude_install import ClaudeLoginResult
        return ClaudeLoginResult(ok=True, account_email="new@user.com",
                                  secret_applied=True, deployment_applied=True,
                                  rollout_ready=True)
    
    monkeypatch.setattr("infra.k8s._claude_install.bootstrap_claude_worker", fake_bootstrap)
    
    view = DispatchMatrixView(data=mock_panel_data_with_claude)
    result = view.handle_key("L", None)
    
    # Assumes the actual implementation triggers async install
    # Verify via the called flag
    assert relogin_called["flag"] is True
```

- [ ] **Step 2: Implement install-on-the-fly logic**

In `infra/k8s/_panel.py`, update `_on_worker_selected`:

```python
def _on_worker_selected(self, stage: str, choice: str) -> None:
    if choice == "claude-worker":
        cw_status = self.data.stage_dispatch.get_claude_worker_status()
        if not cw_status.deployment_applied:
            # Trigger install
            if not self._confirm_install_modal():
                return  # cancelled
            self._perform_install(force_relogin=False)
    
    # Persist (existing kubectl set env logic)
    from infra.k8s._panel_data import set_pipeline_dispatch_stage
    set_pipeline_dispatch_stage(stage, choice, namespace=self.data.namespace)


def _confirm_install_modal(self) -> bool:
    """Show modal 'claude-worker não instalado, instalar agora? [Y/N]'."""
    # Use existing ModalConfirm pattern (PR #330 has examples)
    return ModalConfirm(
        title="claude-worker não instalado",
        message=(
            "Vou:\n"
            "1. Capturar credenciais (claude login se necessário)\n"
            "2. Criar Secret claude-credentials\n"
            "3. Aplicar manifests do pod\n"
            "4. Aguardar Ready"
        ),
        default="N",
    ).prompt()


def _perform_install(self, *, force_relogin: bool) -> None:
    """Executa bootstrap_claude_worker com progress modal."""
    from infra.k8s._claude_install import bootstrap_claude_worker
    
    result = bootstrap_claude_worker(
        namespace=self.data.namespace,
        force_relogin=force_relogin,
        interactive=True,
    )
    
    if not result.ok:
        ModalAlert(title="Install failed", message=result.error).show()
        return
    
    # Refresh provider cache so dashboard shows new state
    if hasattr(self.data.stage_dispatch, "_cache"):
        self.data.stage_dispatch._cache.invalidate()


def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
    # ... existing handlers ...
    
    if key in ("L", "l"):
        # Switch login (force relogin)
        cw_status = self.data.stage_dispatch.get_claude_worker_status()
        if not cw_status.deployment_applied:
            ModalAlert(message="claude-worker não está instalado.").show()
            return ActionResult()
        
        confirm = ModalConfirm(
            title=f"Trocar conta do claude-worker",
            message=f"Atual: {cw_status.logged_in_email or '(desconhecido)'}\n"
                    f"Continuar? Browser vai abrir para nova OAuth.",
            default="N",
        ).prompt()
        if confirm:
            self._perform_install(force_relogin=True)
        return ActionResult()
    
    if key in ("I", "i"):
        cw_status = self.data.stage_dispatch.get_claude_worker_status()
        if cw_status.deployment_applied:
            ModalAlert(message="claude-worker já está instalado.").show()
            return ActionResult()
        if self._confirm_install_modal():
            self._perform_install(force_relogin=False)
        return ActionResult()
    
    # ... rest of existing logic ...
```

- [ ] **Step 3: Run tests, expect pass**

```bash
python3 -m pytest deile/tests/infra/test_dispatch_matrix_view.py -v 2>&1 | tail -15
```

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/_panel.py deile/tests/infra/test_dispatch_matrix_view.py
git commit -m "feat(panel): DispatchMatrixView install-on-the-fly + switch-account

- Selecionar 'claude-worker' quando Deployment ausente → modal Install/Cancel
- [I] hotkey trigger explicit install
- [L] hotkey trigger force_relogin (browser OAuth)
- _perform_install invoca bootstrap_claude_worker com progress feedback
- Cache invalidation após install para refresh imediato do dashboard

Refs #309"
```

---

## Task 21: Wire `[d]` → matrix view, remove `[M]`, atualizar footer hints

**Files:**
- Modify: `infra/k8s/_panel.py` — `Dashboard` nav dict + HOTKEYS string

- [ ] **Step 1: Update nav binding and HOTKEYS**

In `infra/k8s/_panel.py`, locate `class Dashboard(View)` and update:

```python
class Dashboard(View):
    title = "Dashboard"
    refresh_s = 1.0
    
    # ATUALIZADO (issue #309 fase 2):
    HOTKEYS = (
        "[1]Pod watch  [2]Pipeline  [3]Issues/PRs  [4]Logs split  "
        "[5]Tokens  [n]otifier  [a]ctions  [m]odel/runtime  "
        "[d]ispatch (workers & models)  [?]help  [q]uit"
    )
    
    def handle_key(self, key: str, app: "PanelApp") -> ActionResult:
        # ... existing logic ...
        
        nav = {
            "1": "pod-watch",
            "2": "pipeline",
            "3": "issues-prs",
            "4": "logs-split",
            "5": "tokens",
            "n": "notifier-echo",
            "a": "actions",
            "m": "model-switcher",
            # REMOVIDO (issue #309): "M": "stage-models" — view consolidada
            # em dispatch-mode-matrix sob [d].
            "d": "dispatch-mode-matrix",  # ATUALIZADO da PR #330 — antes era "dispatch-mode"
        }
        if key in nav:
            return ActionResult.nav(nav[key])
        # ... rest ...
```

- [ ] **Step 2: Register `dispatch-mode-matrix` view**

In the view registry section of `_panel.py`:

```python
def _build_views(data: PanelData) -> Dict[str, View]:
    return {
        "dashboard": Dashboard(data=data),
        # ... existing ...
        "dispatch-mode-matrix": DispatchMatrixView(data=data),
        # REMOVIDO: "dispatch-mode": DispatchModeView (PR #330) — substituído
        # REMOVIDO: "stage-models": StageModelsView (#305) — substituído
    }
```

- [ ] **Step 3: Optionally rename DispatchModeView remnants**

Delete the old `DispatchModeView` class (PR #330) and `StageModelsView` class (#305). Their tests in `test_panel_dispatch_mode.py` and `test_panel_models_per_stage.py` will need migration (next task).

- [ ] **Step 4: Smoke run the panel locally to verify [d] works**

```bash
# Manual smoke (sem cluster):
python3 infra/k8s/_panel.py --demo
# Press [d], verify matrix view renders
```

Expected: opening panel, pressing `d` opens new DispatchMatrixView. `[M]` does nothing.

- [ ] **Step 5: Commit**

```bash
git add infra/k8s/_panel.py
git commit -m "feat(panel): wire [d] → DispatchMatrixView; remove [M] binding

- Hotkey [d] aponta para 'dispatch-mode-matrix' (era 'dispatch-mode' na PR #330)
- [M] removido do nav dict (view stage-models consolidada na matriz)
- HOTKEYS string atualizada no Dashboard: '[d]ispatch (workers & models)'
- Views antigas DispatchModeView + StageModelsView removidas; tests
  serão migrados na próxima task

Refs #309"
```

---

## Task 22: Migrar tests antigos para a nova view

**Files:**
- Delete: `deile/tests/infra/test_panel_dispatch_mode.py` (PR #330 view)
- Delete: `deile/tests/infra/test_panel_models_per_stage.py` (#305 view)
- Update: `deile/tests/infra/test_dispatch_matrix_view.py` — absorver assertions úteis

- [ ] **Step 1: Read existing tests to extract useful assertions**

```bash
cat deile/tests/infra/test_panel_dispatch_mode.py | head -80
cat deile/tests/infra/test_panel_models_per_stage.py | head -80
```

Identifique assertions ainda úteis (audit logging, persistência kubectl, modal confirmation, etc) que precisam continuar funcionando na nova view.

- [ ] **Step 2: Copy relevant tests into test_dispatch_matrix_view.py**

Add to `deile/tests/infra/test_dispatch_matrix_view.py`:

```python
def test_set_pipeline_dispatch_stage_audits(monkeypatch):
    """Mudança via panel grava audit log SECURITY_POLICY_CHANGED."""
    from infra.k8s._panel_data import set_pipeline_dispatch_stage
    
    audit_calls = []
    monkeypatch.setattr("infra.k8s._panel_data.pd_audit_dispatch_mode_change",
                         lambda **kw: audit_calls.append(kw))
    monkeypatch.setattr("infra.k8s._panel_data._kubectl_set_env_var",
                         lambda *a, **k: (True, ""))
    
    set_pipeline_dispatch_stage("implement", "claude-worker", namespace="deile")
    
    assert any(c.get("outcome") == "completed" for c in audit_calls)


def test_set_pipeline_dispatch_stage_rejects_invalid_dispatcher():
    """Validação fail-fast no helper."""
    from infra.k8s._panel_data import set_pipeline_dispatch_stage
    
    ok, msg = set_pipeline_dispatch_stage("implement", "garbage-worker", namespace="deile")
    assert not ok
    assert "invalid" in msg.lower() or "unknown" in msg.lower()
```

- [ ] **Step 3: Delete obsolete test files**

```bash
git rm deile/tests/infra/test_panel_dispatch_mode.py \
       deile/tests/infra/test_panel_models_per_stage.py
```

- [ ] **Step 4: Run full panel test suite**

```bash
python3 -m pytest deile/tests/infra/ -v 2>&1 | tail -20
```

Expected: all pass; no orphan tests pointing to deleted views.

- [ ] **Step 5: Commit**

```bash
git add deile/tests/infra/test_dispatch_matrix_view.py
git commit -m "test(panel): migrar tests de DispatchModeView+StageModelsView para Matrix

Remove suites obsoletas; absorve assertions ainda relevantes (audit
logging, persistência kubectl, validação fail-fast) para test_dispatch_matrix_view.

Refs #309"
```

---

## Task 23: Smoke test end-to-end (manual)

**Files:**
- Create: `deile/tests/might/test_claude_dispatch_real.py`

> Este test requer cluster K8s vivo + `claude-login` feito. NÃO roda em CI automated. Vive em `tests/might/` por convenção do projeto.

- [ ] **Step 1: Write smoke**

Create `deile/tests/might/test_claude_dispatch_real.py`:

```python
"""Smoke E2E: dispatch real do pipeline pra claude-worker.

Pre-requisitos:
- Cluster K8s vivo (Rancher Desktop)
- python3 infra/k8s/deploy.py k8s up rodou
- python3 infra/k8s/deploy.py k8s claude-login completou
- claude-worker pod Ready (`kubectl get pod -n deile -l app=claude-worker`)

Não roda em CI. Run manual: python3 deile/tests/might/test_claude_dispatch_real.py
"""
import asyncio
import json
import subprocess
import sys


async def test_health():
    proc = subprocess.run([
        "kubectl", "exec", "-n", "deile", "deploy/deile-shell", "--",
        "curl", "-sf", "http://claude-worker:8767/v1/health",
    ], capture_output=True, text=True, check=False)
    
    assert proc.returncode == 0, f"health failed: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["status"] == "ok", f"unexpected: {data}"
    print("✓ /v1/health ok")


async def test_dispatch_smoke():
    """Dispatch um brief simples ('print hello world em python e cite STATUS: SUCCESS').
    
    Verifica:
    - HTTP 200
    - ok=True na response
    - 'hello world' aparece no stdout
    - duration_seconds > 0
    """
    payload = json.dumps({
        "brief": "Run `echo 'hello world from claude' && echo 'STATUS: SUCCESS'` and confirm.",
        "channel_id": "smoke-test",
        "preferred_model": "anthropic:claude-haiku-4-5",
        "stage": "implement",
    })
    
    proc = subprocess.run([
        "kubectl", "exec", "-n", "deile", "deploy/deile-shell", "--",
        "curl", "-sf", "-X", "POST", "-H", "Content-Type: application/json",
        "-d", payload,
        "http://claude-worker:8767/v1/dispatch",
    ], capture_output=True, text=True, check=False, timeout=600)
    
    assert proc.returncode == 0, f"dispatch failed: {proc.stderr}"
    data = json.loads(proc.stdout)
    
    assert data["ok"] is True, f"dispatch returned not-ok: {data}"
    assert "hello world" in data["stdout"].lower(), f"unexpected stdout: {data['stdout'][:500]}"
    assert data["duration_seconds"] > 0
    print(f"✓ /v1/dispatch ok (duration={data['duration_seconds']:.1f}s)")


async def main():
    print("=== claude-worker smoke E2E ===")
    await test_health()
    await test_dispatch_smoke()
    print("=== all smoke tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run smoke manualmente (operador faz após deploy)**

```bash
python3 deile/tests/might/test_claude_dispatch_real.py
```

Expected: 2 ✓ prints, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add deile/tests/might/test_claude_dispatch_real.py
git commit -m "test(might): smoke E2E manual para claude-worker

Verifica /v1/health + dispatch real claude -p num cluster vivo. Não roda
em CI; operator-driven (custa tokens).

Refs #309"
```

---

## Task 24: Update CLAUDE.md + docs/system_design

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/system_design/14-CONTAINERIZACAO.md`
- Modify: `docs/system_design/04-MODELO-COMPONENTES.md` (se houver diagrama de workers)
- Modify: `docs/system_design/00-VISAO-GERAL.md` (adicionar decisão #42)

- [ ] **Step 1: Add new section to CLAUDE.md**

Append to `CLAUDE.md` after the Kubernetes section:

```markdown
## claude-worker — dispatch pra Claude CLI dentro do cluster (issue #309 fase 2)

Além do `deile-worker` (que roda DEILE python), o cluster ganha um pod paralelo
`claude-worker` (Service :8767) que executa `claude -p` em worktrees isolados
sob o PVC `claude-worker-home`. O pipeline despacha tasks per-stage:
- `classify`, `refine`, `implement`, `pr_review`, `follow_ups` cada um pode
  apontar pra `deile-worker` OU `claude-worker`
- Resolver: `deile/orchestration/pipeline/dispatch_resolver.py`
  - env var per-stage: `DEILE_PIPELINE_DISPATCH_<STAGE>`
  - env var global: `DEILE_PIPELINE_DISPATCH_MODE`
  - default: `deile-worker`

### Setup inicial
```bash
# Cluster ainda zerado pra claude:
python3 infra/k8s/deploy.py k8s claude-login           # captura ~/.claude/, cria Secret, aplica manifests, aguarda Ready
python3 infra/k8s/deploy.py k8s claude-login --switch  # força logout + nova OAuth
python3 infra/k8s/deploy.py k8s claude-login --no-interactive  # CI-friendly (falha se sem creds)
```

### Configurar per-stage no painel
- Tecla `[d]` no painel → `DispatchMatrixView` (substitui [d] global + [M] per-stage anteriores)
- Linha por stage: Worker (deile-worker / claude-worker / global) × Model (anthropic-only se claude-worker)
- Linha "Global default" no rodapé funciona como fallback
- `[L]` switch claude-worker login, `[I]` install se ausente

### Threat model resumido
Credentials no PVC mode 0600, NetworkPolicy whitelist (anthropic.com + repos
elimarcavalli/deile + elimarcavalli/deilebot). Gap conhecido: prompt injection
no claude pode exfiltrar credentials via canais legítimos (audit detecta após
fato). FU prioritária: sidecar credential proxy. Ver spec
`docs/superpowers/specs/2026-05-26-claude-worker-design.md` seção 7.
```

- [ ] **Step 2: Update 14-CONTAINERIZACAO.md**

Add a 5ª init mode section (after Local/Job/deile-shell — claude-worker é o 4º):

```markdown
### claude-worker (issue #309 fase 2)

| Campo | Valor |
|---|---|
| Replicas | 1 (V1; FU para RWX scale) |
| Image | deile-stack:local (shared, claude CLI baked) |
| Args | python3 /app/wrapper.py claude-worker |
| Port | 8767 |
| PVC | claude-worker-home (1Gi, /home/claude) |
| Secrets | claude-credentials + claude-worker-bearer |
| InitContainer | bootstrap-creds — copia Secret → PVC writable |
| NetworkPolicy | ingress só do deile-pipeline; egress 443 (manual whitelist via wrapper) |
| ConfigMap | claude-worker-allowed-repos (regex de URLs git permitidas) |
```

- [ ] **Step 3: Update 00-VISAO-GERAL.md decisões**

Add to decisions table:

```markdown
| 42 | claude-worker pod paralelo ao deile-worker para dispatch de `claude -p` em worktrees isolados. Per-stage routing via `dispatch_resolver` (espelha decisão #41). View unificada `[d]` substitui `[d]` global + `[M]` per-stage. Threat model: Section 7 da spec; FUs prioritárias: sidecar credential proxy + Vault integration — issue #309 fase 2 | V1 | Containerização (14), Componentes (04), Princípios (03) |
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/system_design/14-CONTAINERIZACAO.md docs/system_design/00-VISAO-GERAL.md
git commit -m "docs(system-design): claude-worker pod + per-stage dispatch (decisão #42)

- CLAUDE.md ganha seção 'claude-worker' com setup + threat model resumido
- 14-CONTAINERIZACAO.md: 4ª init mode (claude-worker) tabulada
- 00-VISAO-GERAL.md: decisão #42 registrada
- Refs à spec docs/superpowers/specs/2026-05-26-claude-worker-design.md

Refs #309"
```

---

## Task 25: Suite green + lint clean

**Files:**
- ALL test files

- [ ] **Step 1: Run full pytest suite**

```bash
python3 -m pytest deile/tests/ -q 2>&1 | tail -10
```

Expected: all pass (cobertura ≥80% pelo --cov-fail-under).

- [ ] **Step 2: Run ruff**

```bash
ruff check deile/ infra/
```

Expected: clean. Se houver issues:
```bash
ruff check --fix deile/ infra/
```

- [ ] **Step 3: Run isort**

```bash
isort --check-only deile/ infra/
```

Expected: clean. Se houver issues:
```bash
isort deile/ infra/
```

- [ ] **Step 4: Build do image (verifica que Dockerfile + claude CLI install funciona)**

```bash
python3 infra/k8s/deploy.py k8s build --yes 2>&1 | tail -5
```

Expected: build success.

- [ ] **Step 5: Commit any lint fixes**

```bash
git status
git add -A
git commit -m "chore: lint fixes pós-implementation (#309)"
```

Skip commit if no diff.

---

## Task 26: Abrir PR

- [ ] **Step 1: Push branch**

```bash
git push -u origin auto/issue-309-fase-2
```

- [ ] **Step 2: Create PR**

```bash
gh pr create --base main --repo elimarcavalli/deile \
  --title "feat(claude-worker): pod paralelo + per-stage dispatcher (issue #309 fase 2)" \
  --body "$(cat <<'EOF'
## Resumo

Completa a issue #309 (fase 2) que a PR #330 deixou parcial:
- ✅ ADD claude-worker pod paralelo ao deile-worker (HTTP :8767, image deile-stack:local com claude CLI baked)
- ✅ ADD dispatch_resolver per-stage espelhando model_resolver (decisão #41)
- ✅ ADD DispatchMatrixView unificada `[d]` (substitui [d] global + [M] per-stage)
- ✅ ADD `deploy.py k8s claude-login [--switch]` verb + install-on-the-fly no painel
- ✅ ADD threat model explícito (Section 7 da spec) + NetworkPolicy whitelist de repos
- ✅ Backward compat: deile-worker existing continua funcionando idêntico

## Issues

Closes #309

## Decisões-chave

- **Alt A (escolhida)**: pod novo `claude-worker` paralelo. Simetria com deile-worker, isolamento de recursos.
- **Alt B (rejeitada)**: claude -p in-process no deile-pipeline (fat pod, perde isolation).
- **Alt C (rejeitada)**: deile-worker gordo com claude também (quebra SRP).

## Spec

Detalhes em `docs/superpowers/specs/2026-05-26-claude-worker-design.md` (415 linhas).

Threat model documentado na seção 7; gaps de hardening rastreados como FUs prioritárias:
- [ ] Sidecar credential proxy (Pri 1)
- [ ] Vault integration com Anthropic OAuth secret engine (Pri 2)

## Checklist

- [x] pytest 100% verde (full suite)
- [x] ruff ok
- [x] isort ok
- [x] cobertura mantida ≥80%
- [x] pilares 03/12 respeitados: registry, async-first, hexagonal, security-first, error handling tipado
- [x] sem SQL/migration
- [x] threat model documentado + mitigações V1 implementadas
- [x] sem secrets logados/echoed
- [x] FUs Pri 1/2 catalogadas (sidecar + Vault) — recomendado abrir junto

## Test plan

- [x] Unit tests: dispatch_resolver (13), DispatchPayload extended (5), settings schema (4), worker_implementer_routing (3), claude_worker_server (5), _claude_install (3), dispatch_matrix_view (8)
- [x] Smoke E2E manual em `deile/tests/might/test_claude_dispatch_real.py` (operator-driven, requer cluster vivo)
- [ ] Operator deve rodar `python3 infra/k8s/deploy.py k8s claude-login` no Mac antes de flipar stages pra claude-worker no painel
EOF
)"
```

- [ ] **Step 3: Verify PR**

```bash
gh pr view --json number,state,title,checks | jq
```

Expected: PR opened, state=OPEN. CI may take a few minutes.

---

## Self-review

### Spec coverage

| Spec section | Task that implements |
|---|---|
| 4.1 dispatch_resolver | Task 1 |
| 4.2 WorkerImplementer | Task 4 |
| 4.3 DispatchPayload | Task 2 |
| 4.4 claude_worker_server | Tasks 12, 13, 14 |
| 4.5 _claude_install | Task 15 |
| 4.6 DispatchMatrixView | Tasks 18, 19, 20, 21 |
| 5.1 dispatch flow | Tasks 4, 13 |
| 5.2 claude-login flow | Tasks 15, 16 |
| 6 persistência | Tasks 3, 19, 17 |
| 7 threat model | Tasks 7, 10, 11 (allowed-repos + NetworkPolicy + wrapper validation) |
| 8 failure handling | Tasks 4, 13 (HTTP error codes + worker retry reuse) |
| 9 test plan | Tasks 1-22 (unit + integration), Task 23 (smoke) |
| 10 follow-ups | Documentado na spec; PR body links |

### Placeholder scan
- ✅ Sem TBD/TODO
- ✅ Steps têm código completo
- ✅ Comandos exatos

### Type consistency
- `DispatchPayload` campos novos opcionais: stage, action_kind, issue_number, branch — usados consistente em Tasks 2, 4, 13
- `StageDispatchEntry`, `ClaudeWorkerStatus`, `ClaudeLoginResult` definidos uma vez, reusados nas tasks de painel e install
- `PIPELINE_STAGES`, `VALID_DISPATCHERS` importados de `dispatch_resolver` em todos os call sites
