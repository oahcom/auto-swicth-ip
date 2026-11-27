from asip.failure_domain import FailureDomain
from asip.rotation_policy import (
    rotation_policy,
    _LEGACY,
    _NON_EXIT,
    _UNCLASSIFIED,
)


def test_enforced_exit_ip_is_allowed():
    assert rotation_policy(FailureDomain.EXIT_IP, enforcement=True).allow_switch


def test_enforced_invalid_domain_remains_blocked():
    assert rotation_policy("not-a-failure-domain", enforcement=True) == _UNCLASSIFIED


def test_enforcement_disabled_always_keeps_legacy():
    assert rotation_policy(FailureDomain.ACCOUNT, enforcement=False) == _LEGACY
