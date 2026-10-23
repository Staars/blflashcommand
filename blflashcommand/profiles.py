"""Chip identity and asset selection without importing the eager libs package."""

from typing import NamedTuple


class ChipProfile(NamedTuple):
    """Keep the requested firmware identity separate from backend resources."""

    requested_name: str
    resource_name: str
    backend: str
    default_xtal: str


CHIP_PROFILES = {
    'bl602': ChipProfile('bl602', 'bl602', 'bl602', '40m'),
    'bl702': ChipProfile('bl702', 'bl702', 'bl702', '32m'),
    'bl702l': ChipProfile('bl702l', 'bl702l', 'bl702l', '32m'),
    'bl808': ChipProfile('bl808', 'bl808', 'bl808', 'auto'),
    'bl606p': ChipProfile('bl606p', 'bl606p', 'bl808', 'auto'),
    'bl616': ChipProfile('bl616', 'bl616', 'bl616', 'auto'),
    # BL618 shares the BL616 bootrom protocol/backend but needs its own
    # SDK-recommended flash timing and resource configuration.
    'bl618': ChipProfile('bl618', 'bl618', 'bl616', 'auto'),
}


def resolve_chip(name):
    """Resolve a case-insensitive CLI chip name, rejecting unknown profiles."""
    if not isinstance(name, str):
        raise ValueError('Chip name must be a string')
    try:
        return CHIP_PROFILES[name.lower()]
    except KeyError:
        raise ValueError('Unsupported chip name: ' + name) from None
