"""Tests for :func:`deile.orchestration.forge.detect_forge_kind` and friends."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from deile.orchestration.forge import (
    ForgeDetectionError,
    ForgeKind,
    declared_hosts,
    detect_forge_kind,
)
from deile.orchestration.forge.detection import _probe_cache, _probe_host


def test_detect_explicit_override_wins_github():
    # Even with a GitLab URL, the explicit override decides.
    kind = detect_forge_kind(
        url="https://gitlab.com/g/p/-/issues/1",
        env={"DEILE_FORGE_KIND": "github"},
    )
    assert kind is ForgeKind.GITHUB


def test_detect_explicit_override_wins_gitlab():
    kind = detect_forge_kind(
        url="https://github.com/o/r/pull/1",
        env={"DEILE_FORGE_KIND": "gitlab"},
    )
    assert kind is ForgeKind.GITLAB


def test_detect_from_url_github_cloud():
    assert detect_forge_kind(url="https://github.com/o/r", env={}) is ForgeKind.GITHUB


def test_detect_from_url_gitlab_cloud():
    assert detect_forge_kind(url="https://gitlab.com/g/p", env={}) is ForgeKind.GITLAB


def test_detect_from_env_custom_github_host():
    env = {"DEILE_GITHUB_HOST": "ghe.empresa.com"}
    assert (
        detect_forge_kind(
            url="https://ghe.empresa.com/team/svc/pull/1",
            env=env,
        )
        is ForgeKind.GITHUB
    )


def test_detect_from_env_custom_gitlab_host():
    env = {"DEILE_GITLAB_HOST": "gitlab.empresa.com"}
    assert (
        detect_forge_kind(
            url="https://gitlab.empresa.com/team/svc/-/issues/1",
            env=env,
        )
        is ForgeKind.GITLAB
    )


def test_detect_nested_path_resolves_to_gitlab():
    """A 3-segment project path can ONLY be GitLab — unambiguous."""
    assert detect_forge_kind(project_path="g/sub/proj", env={}) is ForgeKind.GITLAB


def test_detect_two_segment_path_defaults_to_github_for_compat():
    """A 2-segment path is ambiguous in principle; defaults to GH to preserve
    the historical behaviour (every pre-#297 deployment was GitHub)."""
    assert detect_forge_kind(project_path="owner/repo", env={}) is ForgeKind.GITHUB


def test_detect_no_signals_fails_fast():
    with pytest.raises(ForgeDetectionError) as exc_info:
        detect_forge_kind(env={})
    msg = str(exc_info.value)
    # Error message MUST name the env vars the operator can set — it is the
    # only way the operator escapes the failure mode.
    assert "DEILE_FORGE_KIND" in msg
    assert "DEILE_GITHUB_HOST" in msg or "DEILE_GITLAB_HOST" in msg


def test_detect_unknown_explicit_kind_raises():
    """An explicit but invalid kind raises a forge-layer error. The exact
    subclass (``ForgeConfigError`` or ``ForgeDetectionError``) is an
    implementation detail; both subclass :class:`ForgeError`."""
    from deile.orchestration.forge import ForgeError

    with pytest.raises(ForgeError):
        detect_forge_kind(env={"DEILE_FORGE_KIND": "bitbucket"})


def test_detect_auto_value_treated_as_unset():
    # The CLI default is ``auto`` (declared in settings); detection must
    # honour it as "use the heuristic" rather than parse it as a kind.
    assert (
        detect_forge_kind(
            url="https://github.com/o/r",
            env={"DEILE_FORGE_KIND": "auto"},
        )
        is ForgeKind.GITHUB
    )


def test_declared_hosts_default():
    assert declared_hosts({"DEILE_GITHUB_HOST": "", "DEILE_GITLAB_HOST": ""}) == {
        "github_hosts": (),
        "gitlab_hosts": (),
    }


def test_declared_hosts_csv():
    result = declared_hosts(
        {
            "DEILE_GITHUB_HOST": "ghe-a.empresa.com, ghe-b.empresa.com",
            "DEILE_GITLAB_HOST": "gitlab.empresa.com",
        }
    )
    assert "ghe-a.empresa.com" in result["github_hosts"]
    assert "ghe-b.empresa.com" in result["github_hosts"]
    assert result["gitlab_hosts"] == ("gitlab.empresa.com",)


# ---------------------------------------------------------------------------
# Testes para o HTTP probe (_probe_host e integração em detect_forge_kind)
# ---------------------------------------------------------------------------


def _make_mock_response(status: int, headers: dict = None):
    """Cria um mock de resposta urllib para uso em testes de probe."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.headers = headers or {}
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


@pytest.mark.unit
async def test_probe_returns_gitlab_when_v4_version_responds() -> None:
    """_probe_host deve retornar GITLAB quando /api/v4/version responde 200."""
    # Limpa o cache entre testes.
    _probe_cache.pop("unknown-gl.empresa.com", None)

    gl_resp = _make_mock_response(200)

    def fake_urlopen(req, timeout=None):
        if "/api/v4/" in req.full_url:
            return gl_resp
        raise Exception("not gitlab")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = await _probe_host("unknown-gl.empresa.com")

    assert result is ForgeKind.GITLAB


@pytest.mark.unit
async def test_probe_returns_github_when_v3_root_responds() -> None:
    """_probe_host deve retornar GITHUB quando /api/v3/ responde 200 (GHES)."""
    import urllib.error

    _probe_cache.pop("unknown-gh.empresa.com", None)

    gh_resp = _make_mock_response(200, headers={"X-GitHub-Enterprise-Version": "3.10"})

    def fake_urlopen(req, timeout=None):
        if "/api/v4/" in req.full_url:
            raise urllib.error.URLError("not gitlab")
        if "/api/v3/" in req.full_url:
            return gh_resp
        raise Exception("unexpected")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = await _probe_host("unknown-gh.empresa.com")

    assert result is ForgeKind.GITHUB


@pytest.mark.unit
async def test_probe_disabled_when_env_var_not_set() -> None:
    """detect_forge_kind NÃO deve chamar probe quando DEILE_FORGE_PROBE não é '1'."""
    _probe_cache.pop("mystery.host.com", None)

    with patch("deile.orchestration.forge.detection._probe_host_sync") as mock_probe:
        with pytest.raises(ForgeDetectionError):
            detect_forge_kind(
                url="https://mystery.host.com/group/repo",
                env={},  # DEILE_FORGE_PROBE ausente
            )
    mock_probe.assert_not_called()


@pytest.mark.unit
async def test_probe_timeout_falls_through_to_error() -> None:
    """Se o probe falhar (timeout/conexão), detect_forge_kind deve levantar ForgeDetectionError."""
    import urllib.error

    _probe_cache.pop("timeout.host.com", None)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("timed out")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(ForgeDetectionError):
            detect_forge_kind(
                url="https://timeout.host.com/owner/repo",
                env={"DEILE_FORGE_PROBE": "1"},
            )


@pytest.mark.unit
async def test_probe_result_is_cached() -> None:
    """O resultado de _probe_host deve ser armazenado em cache para evitar sondas repetidas."""
    _probe_cache.pop("cached.empresa.com", None)

    import urllib.error

    gl_resp = _make_mock_response(200)

    call_count = 0

    def fake_urlopen(req, timeout=None):
        nonlocal call_count
        call_count += 1
        if "/api/v4/" in req.full_url:
            return gl_resp
        raise urllib.error.URLError("not this")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        r1 = await _probe_host("cached.empresa.com")
        # Captura o call_count após a primeira sonda (pode ser 1 ou 2 dependendo
        # de quantas tentativas paralelas foram feitas — GL + GH opcionalmente).
        calls_after_first = call_count
        r2 = await _probe_host("cached.empresa.com")

    assert r1 is ForgeKind.GITLAB
    assert r2 is ForgeKind.GITLAB
    # Segunda chamada deve retornar do cache — sem I/O adicional.
    assert call_count == calls_after_first


@pytest.mark.unit
async def test_probe_returns_none_when_both_fail() -> None:
    """_probe_host retorna None quando ambas as sondas falham."""
    import urllib.error

    _probe_cache.pop("both-fail.empresa.com", None)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = await _probe_host("both-fail.empresa.com")

    assert result is None
