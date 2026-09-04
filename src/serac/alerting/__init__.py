"""M5 alerting: a real, signable CAP 1.2 generator and the sinks that carry its output.

`serac.streaming.cap_stub` remains the stage the Prompt 1 detector feeds; this package is the
forecast lane's alerting path. Nothing here sends anything anywhere by default.
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
