"""Exercise the public certificate checks used before compilation and delivery."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("signing_checks", ROOT / "workspace/scripts/build_provenance.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


class SigningTests(unittest.TestCase):
    def test_release_requires_the_pinned_certificate_even_with_an_override(self):
        baseline, other = P.BASELINE_CERT_SHA256, "b" * 64
        with patch.dict(os.environ, FUXI_SIGNING_MODE="release", FUXI_SIGNING_CERT_SHA256=other):
            P.check_signing_certificate(baseline, baseline)
            for actual, expected in ((other, baseline), (baseline, other), (other, other)):
                with self.subTest(actual=actual, expected=expected), self.assertRaises(ValueError):
                    P.check_signing_certificate(actual, expected)

    def test_ephemeral_requires_exact_digest_and_distinct_certificate(self):
        baseline, generated = P.BASELINE_CERT_SHA256, "b" * 64
        with patch.dict(os.environ, FUXI_SIGNING_MODE="ephemeral", FUXI_SIGNING_CERT_SHA256=generated):
            P.check_signing_certificate(generated, baseline)
            with self.assertRaises(ValueError):
                P.check_signing_certificate("c" * 64, baseline)
            for invalid in ("", "invalid", baseline):
                with self.subTest(invalid=invalid), patch.dict(os.environ, FUXI_SIGNING_CERT_SHA256=invalid):
                    with self.assertRaises(ValueError):
                        P.check_signing_certificate(invalid, baseline)
