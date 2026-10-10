"""Small, readable domain errors; no UI/framework dependency."""


class HealthcareError(Exception):
    """Base class for errors safe to display in the integration UI."""


class ValidationError(HealthcareError):
    pass


class NotFoundError(HealthcareError):
    pass


class AuthorizationError(HealthcareError):
    pass


class ConflictError(HealthcareError):
    pass


class ApprovalRequiredError(HealthcareError):
    pass


class SchemaMismatchError(HealthcareError):
    pass


class PriorityEngineError(HealthcareError):
    pass


class SlotUnavailableError(ConflictError):
    def __init__(self, message, alternatives=None):
        super().__init__(message)
        self.alternatives = alternatives or []
