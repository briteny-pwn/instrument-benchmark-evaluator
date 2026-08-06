from .world_contract import WorldContractError, dump_world, load_world

__all__ = ["BenchContext", "WorldContractError", "dump_world", "load_world"]


def __getattr__(name: str):
    if name == "BenchContext":
        from .bench import BenchContext

        return BenchContext
    raise AttributeError(name)
