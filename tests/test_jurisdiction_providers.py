"""Tests for jurisdiction_providers module (DP#25)."""
import unittest
import warnings
from unittest.mock import patch
from jurisdiction_providers import (
    register_provider, get_provider, clear_providers, _PROVIDERS,
    _auto_register,
)


class TestJurisdictionProviders(unittest.TestCase):
    def test_register_and_get(self):
        """Custom provider registration and retrieval."""
        register_provider('test_custom', {'key': 'value'})
        provider = get_provider('test_custom')
        self.assertEqual(provider['key'], 'value')
        # Clean up
        _PROVIDERS.pop('test_custom', None)

    def test_get_strategies_auto_registered(self):
        """strategies provider is auto-registered from countries.canada."""
        self.assertIn('strategies', _PROVIDERS)
        provider = get_provider('strategies')
        self.assertIsNotNone(provider)

    def test_get_rate_model_auto_registered(self):
        """rate_model provider is auto-registered from countries.canada."""
        self.assertIn('rate_model', _PROVIDERS)
        provider = get_provider('rate_model')
        self.assertIn('RatePath', provider)
        self.assertIn('build_rate_path', provider)

    def test_unknown_provider_raises(self):
        """Unknown provider without fallback raises KeyError."""
        # Use warnings filter to allow the DeprecationWarning, then the
        # fallback will try to import and fail, raising KeyError
        with self.assertRaises(KeyError):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                get_provider('nonexistent_provider_xyz')

    def test_fallback_import_works(self):
        """Fallback import from countries.canada should work without warnings."""
        # Temporarily remove a registered provider to trigger fallback
        saved = _PROVIDERS.pop('strategies', None)
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                result = get_provider('strategies')
                # Should succeed without DeprecationWarning
                dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
                self.assertEqual(len(dep_warnings), 0)
                self.assertIsNotNone(result)
        finally:
            if saved is not None:
                _PROVIDERS['strategies'] = saved

    def test_estate_fallback_import_works(self):
        """Issue #732: the estate provider's get_provider fallback imports
        compute_estate/EstatePlan/EstateResult from countries.canada.estate
        when the provider was not auto-registered. Covers the DP#25 seam the
        optimization layer now resolves estate math through."""
        saved = _PROVIDERS.pop('estate', None)
        try:
            result = get_provider('estate')
            self.assertIn('compute_estate', result)
            self.assertIn('EstatePlan', result)
            self.assertIn('EstateResult', result)
        finally:
            if saved is not None:
                _PROVIDERS['estate'] = saved
            else:
                _auto_register()

    def test_rate_model_fallback_import_works(self):
        """get_provider('rate_model') hits the fallback-import path when the
        provider was not auto-registered (covers the rate_model fallback body)."""
        saved = _PROVIDERS.pop('rate_model', None)
        try:
            result = get_provider('rate_model')
            self.assertIn('RatePath', result)
            self.assertIn('build_rate_path', result)
        finally:
            if saved is not None:
                _PROVIDERS['rate_model'] = saved

    def test_tax_calc_fallback_import_works(self):
        """get_provider('tax_calc') hits the fallback-import path when the
        provider was not auto-registered (covers the tax_calc fallback body)."""
        saved = _PROVIDERS.pop('tax_calc', None)
        try:
            result = get_provider('tax_calc')
            self.assertIsNotNone(result)
        finally:
            if saved is not None:
                _PROVIDERS['tax_calc'] = saved

    def test_clear_providers(self):
        """clear_providers() empties the registry."""
        register_provider('temp_for_clear_test', {'k': 1})
        self.assertIn('temp_for_clear_test', _PROVIDERS)
        clear_providers()
        self.assertNotIn('temp_for_clear_test', _PROVIDERS)
        _auto_register()  # restore real providers for the rest of the session

    def test_get_provider_fallback_import_failure_raises_keyerror(self):
        """If a provider's fallback import fails, get_provider raises KeyError
        via the except-ImportError path."""
        saved = _PROVIDERS.pop('estate', None)
        with patch.dict('sys.modules', {'countries.canada.estate': None}):
            with self.assertRaises(KeyError):
                get_provider('estate')
        if saved is not None:
            _PROVIDERS['estate'] = saved
        else:
            _auto_register()

    def test_auto_register_swallows_import_errors(self):
        """_auto_register() must not raise when a jurisdiction module cannot be
        imported -- it swallows ImportError and leaves that provider absent."""
        clear_providers()
        poisoned = {
            'countries.canada.strategies': None,
            'countries.canada.rate_model': None,
            'countries.canada.estate': None,
        }
        with patch.dict('sys.modules', poisoned):
            _auto_register()  # must not raise
        self.assertNotIn('strategies', _PROVIDERS)
        self.assertNotIn('rate_model', _PROVIDERS)
        self.assertNotIn('estate', _PROVIDERS)
        clear_providers()
        _auto_register()  # restore real providers

    def test_auto_register_no_countries_dir_is_noop(self):
        """_auto_register() early-returns when the countries/ directory is
        absent (covers the `not countries_dir.is_dir()` guard)."""
        import jurisdiction_providers as jp
        clear_providers()
        # Point __file__ at a temp location with no countries/ subdir so the
        # is_dir() guard is False and _auto_register returns early.
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(jp, '__file__', os.path.join(tmp, 'jp.py')):
                jp._auto_register()  # must not raise, must register nothing
        self.assertNotIn('strategies', _PROVIDERS)
        self.assertNotIn('rate_model', _PROVIDERS)
        self.assertNotIn('estate', _PROVIDERS)
        clear_providers()
        _auto_register()  # restore real providers


if __name__ == '__main__':
    unittest.main()