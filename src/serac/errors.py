"""Exception hierarchy shared across serac."""


class SeracError(Exception):
    """Base class for all serac errors."""


class DatasetNotFetchedError(SeracError):
    """A dataset the caller relies on is recorded as `not_fetched`; serac refuses to fake it."""


class CredentialsMissingError(SeracError):
    """An adapter needs a credential that is not configured (see docs/CREDENTIALS.md)."""


class NotImplementedYetError(SeracError):
    """Capability is designed but deliberately not implemented yet."""
