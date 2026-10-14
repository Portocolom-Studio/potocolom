import inspect


def fail_next(monkeypatch, owner, name, exc=None, times=1):
    """Make the next `times` calls of owner.name raise, then call through.

    Returns the list of argument tuples it saw, so a test can assert the call
    happened. The attribute is patched rather than a caller, so every code path
    that looks the name up at call time sees the fault, not just one of them.
    """
    real = getattr(owner, name)
    seen = []

    def check(args):
        seen.append(args)
        if len(seen) <= times:
            raise exc or RuntimeError("injected fault")

    if inspect.iscoroutinefunction(real):
        async def wrapper(*args, **kwargs):
            check(args)
            return await real(*args, **kwargs)
    else:
        def wrapper(*args, **kwargs):
            check(args)
            return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)
    return seen
