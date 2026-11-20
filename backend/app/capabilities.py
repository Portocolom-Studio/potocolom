"""Fixed capabilities for each stored role.

One role stays on the user row. What that role may do is this table, not a
rank. An unknown role holds nothing.
"""

from enum import StrEnum


class Capability(StrEnum):
    GENERATE = "generate"
    EDIT_OWN_WORK = "edit_own_work"
    VIEW_OWN_USAGE = "view_own_usage"
    ADMINISTER = "administer"


ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "viewer": frozenset(),
    "user": frozenset({
        Capability.GENERATE,
        Capability.EDIT_OWN_WORK,
        Capability.VIEW_OWN_USAGE,
    }),
    "admin": frozenset(Capability),
}
PRIVILEGED = frozenset({Capability.ADMINISTER})
WRITE_CAPABILITIES = frozenset({Capability.GENERATE, Capability.EDIT_OWN_WORK})


def capabilities_of(role: str) -> frozenset[Capability]:
    return ROLE_CAPABILITIES.get(role, frozenset())


def generating_roles() -> frozenset[str]:
    return frozenset(role for role, bundle in ROLE_CAPABILITIES.items()
                     if Capability.GENERATE in bundle)


def effective_capabilities(user) -> frozenset[Capability]:
    """What the studio may offer. Enforcement uses the role bundle and the
    existing state checks, not this set."""
    bundle = capabilities_of(user.role)
    if user.state == "active":
        return bundle
    if user.state == "suspended":
        return bundle - WRITE_CAPABILITIES
    return frozenset()
