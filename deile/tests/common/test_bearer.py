"""Testes unitários para deile.common.bearer (issue #765).

Verifica que ``_TOKEN_SAFE_CHARS`` e ``_validate_token_charset`` importam
corretamente de ``deile.common.bearer`` e se comportam conforme o contrato:

- tokens válidos (charset correto, comprimento dentro do intervalo) → True
- tokens inválidos (chars proibidos como CR/LF/NUL, comprimento < 16) → False
"""

from __future__ import annotations

import pytest

from deile.common.bearer import _TOKEN_SAFE_CHARS, _validate_token_charset


class TestTokenSafeCharsRegex:
    """Testes diretos contra a regex compilada ``_TOKEN_SAFE_CHARS``."""

    def test_regex_accepts_alphanumeric_token(self):
        token = "a" * 16
        assert _TOKEN_SAFE_CHARS.match(token) is not None

    def test_regex_accepts_all_allowed_special_chars(self):
        # charset: A-Za-z0-9._\-+/=:~
        token = "abcABC012._-+/=:~" + "x" * 3  # 21 chars, all allowed
        assert _TOKEN_SAFE_CHARS.match(token) is not None

    def test_regex_rejects_token_shorter_than_16(self):
        token = "a" * 15
        assert _TOKEN_SAFE_CHARS.match(token) is None

    def test_regex_accepts_token_exactly_16_chars(self):
        token = "a" * 16
        assert _TOKEN_SAFE_CHARS.match(token) is not None

    def test_regex_accepts_token_exactly_4096_chars(self):
        token = "a" * 4096
        assert _TOKEN_SAFE_CHARS.match(token) is not None

    def test_regex_rejects_token_longer_than_4096(self):
        token = "a" * 4097
        assert _TOKEN_SAFE_CHARS.match(token) is None

    def test_regex_rejects_empty_string(self):
        assert _TOKEN_SAFE_CHARS.match("") is None

    def test_regex_rejects_cr_char(self):
        token = "a" * 15 + "\r"
        assert _TOKEN_SAFE_CHARS.match(token) is None

    def test_regex_rejects_lf_char(self):
        token = "a" * 15 + "\n"
        assert _TOKEN_SAFE_CHARS.match(token) is None

    def test_regex_rejects_nul_char(self):
        token = "a" * 15 + "\x00"
        assert _TOKEN_SAFE_CHARS.match(token) is None

    def test_regex_rejects_space(self):
        token = "a" * 15 + " "
        assert _TOKEN_SAFE_CHARS.match(token) is None

    def test_regex_rejects_tab(self):
        token = "a" * 15 + "\t"
        assert _TOKEN_SAFE_CHARS.match(token) is None


class TestValidateTokenCharset:
    """Testes funcionais para ``_validate_token_charset``."""

    def test_returns_true_for_valid_token(self):
        assert _validate_token_charset("ValidToken12345678") is True

    def test_returns_false_for_short_token(self):
        assert _validate_token_charset("short") is False

    def test_returns_false_for_empty_string(self):
        assert _validate_token_charset("") is False

    def test_returns_false_for_token_with_cr(self):
        assert _validate_token_charset("validtoken12345\r") is False

    def test_returns_false_for_token_with_lf(self):
        assert _validate_token_charset("validtoken12345\n") is False

    def test_returns_false_for_token_with_nul(self):
        assert _validate_token_charset("validtoken12345\x00") is False

    def test_returns_true_for_token_with_all_safe_special_chars(self):
        # Monta um token usando todos os caracteres especiais permitidos
        token = "ABCabc012._-+/=:~"  # 18 chars, all safe
        assert _validate_token_charset(token) is True

    def test_returns_false_for_token_with_at_sign(self):
        # '@' não está no charset permitido
        assert _validate_token_charset("validtoken12345@") is False

    def test_returns_false_for_token_with_exclamation(self):
        assert _validate_token_charset("validtoken12345!") is False

    def test_returns_true_for_realistic_bearer_token(self):
        # Formato típico de token gerado por secrets.token_urlsafe(32)
        token = "abc123-DEF456_xyz789.ABCDEFGHIJKLMNOPQRSTU="  # gitleaks:allow (token fake p/ teste)
        assert _validate_token_charset(token) is True

    def test_return_type_is_bool(self):
        result = _validate_token_charset("a" * 16)
        assert isinstance(result, bool)
