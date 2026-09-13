"""M5 alerting: a real, signable CAP 1.2 generator and the sinks that carry its output.

The replay and live stream lanes default to `serac.streaming.cap_stage`, which renders
detection-path messages as `status=Test` and forecast-path messages through `build_alert`.
`serac.streaming.cap_stub` stays in the tree for `--cap stub` and fixtures that pin stub XML.
Nothing here sends anything anywhere by default.
"""

from serac.alerting.generator import AlertBuild, build_alert
from serac.alerting.keys import generate_keypair, public_key_fingerprint
from serac.alerting.signing import sign_cap_xml, verify_cap_signature

__all__ = [
    "AlertBuild",
    "build_alert",
    "generate_keypair",
    "public_key_fingerprint",
    "sign_cap_xml",
    "verify_cap_signature",
]
