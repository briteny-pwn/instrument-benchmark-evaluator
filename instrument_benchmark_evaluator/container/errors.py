class ContainerContractError(ValueError):
    """The instance container contract is invalid or incompatible."""


class ImagePolicyError(ContainerContractError):
    """The declared Dockerfile or image violates evaluator policy."""


class ContainerInfrastructureError(RuntimeError):
    """Docker or host container infrastructure failed."""


class ContainerCommandTimeout(ContainerInfrastructureError):
    """A bounded Docker command exceeded its deadline."""
