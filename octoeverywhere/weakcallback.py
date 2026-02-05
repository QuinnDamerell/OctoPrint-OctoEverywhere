import weakref
from typing import Callable, Any, Optional, TypeVar, Generic

# TypeVar bound to Callable so WeakCallback[Callable[[X], Y]] preserves the signature.
C = TypeVar("C", bound=Callable[..., Any])


# This class is a helper to wrap callbacks in a weak reference to prevent circular references that can lead to memory leaks.
class WeakCallback(Generic[C]):
    def __init__(self, callback: C) -> None:
        try:
            # Attempt to create a weak reference to a bound method
            self._weakRef: Optional[weakref.WeakMethod[C]] = weakref.WeakMethod(callback)
            self._is_weak = True
            self._ref: Optional[C] = None
        except TypeError:
            # Not a bound method (plain function), store strong ref
            self._weakRef = None
            self._ref: Optional[C] = callback
            self._is_weak = False


    # This will return None if the callback is a weak reference and has been garbage collected, or if it was a weak reference to begin with and the original object is gone. Otherwise, it returns the original callback.
    def GetStrongRef(self) -> Optional[C]:
        if self._is_weak and self._weakRef is not None:
            return self._weakRef()
        else:
            return self._ref


    # This can be used if the return type doesn't matter, because it will return None if the callback is dead.
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        cb = self.GetStrongRef()
        if cb is None:
            return None
        return cb(*args, **kwargs)
