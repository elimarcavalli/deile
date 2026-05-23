import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.cli import _silence_genai_shutdown_noise


@pytest.fixture
def mock_google_genai():
    """Mock google.genai.client module."""
    with patch.dict(sys.modules):
        mock_client_module = MagicMock()
        
        class MockClient:
            def __del__(self):
                pass
                
        class MockBaseApiClient:
            async def aclose(self):
                pass
                
        mock_client_module.Client = MockClient
        mock_client_module.BaseApiClient = MockBaseApiClient
        
        sys.modules["google.genai"] = MagicMock(client=mock_client_module)
        yield mock_client_module


def test_silence_genai_shutdown_noise_no_module():
    """Test when google.genai is not installed."""
    with patch.dict(sys.modules):
        if "google.genai" in sys.modules:
            del sys.modules["google.genai"]
        
        # Should not raise ImportError
        _silence_genai_shutdown_noise()


def test_silence_genai_shutdown_noise_client_del(mock_google_genai):
    """Test that Client.__del__ is patched and swallows exceptions."""
    # Setup original __del__ to raise an exception
    original_del_called = False
    
    def failing_del(self):
        nonlocal original_del_called
        original_del_called = True
        raise AttributeError("mock error")
        
    mock_google_genai.Client.__del__ = failing_del
    
    # Apply patch
    _silence_genai_shutdown_noise()
    
    # Call patched __del__
    client_instance = mock_google_genai.Client()
    client_instance.__del__()
    
    # Verify original was called but exception was swallowed
    assert original_del_called is True


@pytest.mark.asyncio
async def test_silence_genai_shutdown_noise_base_api_client_aclose(mock_google_genai):
    """Test that BaseApiClient.aclose is patched and swallows AttributeError."""
    # Setup original aclose to raise an exception
    original_aclose_called = False
    
    async def failing_aclose(self):
        nonlocal original_aclose_called
        original_aclose_called = True
        raise AttributeError("'_async_httpx_client' not found")
        
    mock_google_genai.BaseApiClient.aclose = failing_aclose
    
    # Apply patch
    _silence_genai_shutdown_noise()
    
    # Call patched aclose
    client_instance = mock_google_genai.BaseApiClient()
    await client_instance.aclose()
    
    # Verify original was called but exception was swallowed
    assert original_aclose_called is True


@pytest.mark.asyncio
async def test_silence_genai_shutdown_noise_base_api_client_aclose_other_exception(mock_google_genai):
    """Test that BaseApiClient.aclose swallows other exceptions too."""
    original_aclose_called = False
    
    async def failing_aclose(self):
        nonlocal original_aclose_called
        original_aclose_called = True
        raise ValueError("some other error")
        
    mock_google_genai.BaseApiClient.aclose = failing_aclose
    
    # Apply patch
    _silence_genai_shutdown_noise()
    
    # Call patched aclose
    client_instance = mock_google_genai.BaseApiClient()
    await client_instance.aclose()
    
    # Verify original was called but exception was swallowed
    assert original_aclose_called is True


@pytest.mark.asyncio
async def test_silence_genai_shutdown_noise_idempotent(mock_google_genai):
    """Test that the patch is idempotent and doesn't wrap multiple times."""
    # Apply patch twice
    _silence_genai_shutdown_noise()
    first_patch = mock_google_genai.BaseApiClient.aclose
    
    _silence_genai_shutdown_noise()
    second_patch = mock_google_genai.BaseApiClient.aclose
    
    # Should be the exact same function reference
    assert first_patch is second_patch
    assert first_patch.__name__ == "_safe_aclose"
