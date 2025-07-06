from typing import Optional, Union

# Define a type for bytes like objects, for ease of use.
BufferOrNone = Union["Buffer", None]
ByteLike = Union[bytes, bytearray]
ByteLikeOrMemoryView = Union[bytes, bytearray, memoryview]
ByteLikeOrNone = Union[ByteLike, None]

# An abstraction around bytes, bytearray, and such to make working with them and between them the most efficient as possible.
#
# Why? bytes is an immutable type, which isn't super useful. It's also an older type compared to bytearray an memoryview.
# But, to convert from a bytearray to bytes, we need to copy the data. This is inefficient.
#
# So, if we can get away with using bytes all the way through, we will. But if we need to modify the data, we need to convert it to a bytearray.
#
# Thus the goal of this class is to abstract that logic away, and always internally do the right according to the usage.
# Ideally this buffer class should be passed around inside of the code, and only the endpoints should be using the bytes or bytearray types.
class Buffer():

    # This must wrap a real buffer, otherwise it should just be None.
    def __init__(self, data:ByteLikeOrMemoryView) -> None:
        self._bytes:Optional[bytes] = None
        self._bytearray:Optional[bytearray] = None
        self._memoryview:Optional[memoryview] = None

        # Set the correct var depending on the type of data.
        if isinstance(data, bytes):
            self._bytes = data
        elif isinstance(data, bytearray):
            self._bytearray = data
        elif isinstance(data, memoryview):
            self._memoryview = data
        else:
            raise TypeError("data must be bytes, bytearray, memoryview or None")


    # This should be used in most cases to get the buffer only for reading.
    # In this case, either bytes or bytearray will be returned, which ever currently exists.
    def Get(self) -> ByteLikeOrMemoryView:
        if self._bytes is not None:
            return self._bytes
        elif self._bytearray is not None:
            return self._bytearray
        elif self._memoryview is not None:
            return self._memoryview
        else:
            raise ValueError("Buffer is empty")


    # Should be used when we need to get the buffer as a bytes or bytearray.
    # If the underlying type is a memoryview, it will be converted to bytes or bytearray.
    # For example, this is needed when .decode() is called.
    def GetBytesLike(self) -> ByteLike:
        if self._bytes is not None:
            return self._bytes
        elif self._bytearray is not None:
            return self._bytearray
        elif self._memoryview is not None:
            # We need to convert the memoryview to bytes.
            # This is a copy of the data, so it will be slow.
            return self.ForceAsByteArray()
        else:
            raise ValueError("Buffer is empty")


    # Only used when required, otherwise use Get.
    # This function will force the underlying buffer to be converted to bytes, so it can be used.
    # ONLY use this when you absolutely need to get the buffer as bytes.
    # This does nothing if the type is already bytes.
    def ForceAsBytes(self) -> bytes:
        if self._bytes is not None:
            return self._bytes
        elif self._bytearray is not None:
            # Convert the bytearray to bytes.
            self._bytes = bytes(self._bytearray)
            self._bytearray = None
            return self._bytes
        elif self._memoryview is not None:
            self._bytes = bytes(self._memoryview)
            self._memoryview = None
            return self._bytes
        else:
            raise ValueError("Buffer is empty")


    # Only used when required, otherwise use Get.
    # This function will force the underlying buffer to be converted to a bytearray, so it can be used.
    # ONLY use this when you absolutely need to get the buffer as a bytearray.
    # This does nothing if the type is already bytearray.
    def ForceAsByteArray(self) -> bytearray:
        if self._bytearray is not None:
            return self._bytearray
        elif self._bytes is not None:
            # Convert the bytes to bytearray.
            self._bytearray = bytearray(self._bytes)
            self._bytes = None
            return self._bytearray
        elif self._memoryview is not None:
            # Convert the memoryview to bytearray.
            self._bytearray = bytearray(self._memoryview)
            self._memoryview = None
            return self._bytearray
        else:
            raise ValueError("Buffer is empty")


    # This should be used when we need to modify the buffer.
    def ConvertToEditableBuffer(self) -> None:
        # If we have a bytes object, we need to convert it to a bytearray.
        if self._bytes is not None:
            self._bytearray = bytearray(self._bytes)
            self._bytes = None
            return
        elif self._bytearray is not None:
            return
        elif self._memoryview is not None:
            return
        else:
            raise ValueError("Buffer is empty")


    # This doesn't free the memory, but if there's a memory view it releases it, allowing the buffer it was wrapping the ability to be resized.
    def Release(self) -> None:
        # If we have a bytes object, we need to convert it to a bytearray.
        if self._bytes is not None:
            self._bytes = None
            return
        elif self._bytearray is not None:
            self._bytearray = None
            return
        elif self._memoryview is not None:
            self._memoryview.release()
            self._memoryview = None
            return
        else:
            raise ValueError("Buffer is empty")


    # Allow the len function to work.
    def __len__(self) -> int:
        if self._bytes is not None:
            return len(self._bytes)
        elif self._bytearray is not None:
            return len(self._bytearray)
        elif self._memoryview is not None:
            return len(self._memoryview)
        else:
            return 0


    # Allow the buffer to be iterated over.
    def __iter__(self):
        if self._bytes is not None:
            return iter(self._bytes)
        elif self._bytearray is not None:
            return iter(self._bytearray)
        elif self._memoryview is not None:
            return iter(self._memoryview)
        else:
            return iter([])


    # Allow the buffer to be indexed or sliced.
    def __getitem__(self, key:Union[int, slice]) -> Union[int, ByteLikeOrMemoryView]:
        if isinstance(key, slice):
            if self._bytes is not None:
                return self._bytes[key]
            elif self._bytearray is not None:
                return self._bytearray[key]
            elif self._memoryview is not None:
                return self._memoryview[key]
            else:
                raise ValueError("Buffer is empty")
        else:
            if self._bytes is not None:
                return self._bytes[key]
            elif self._bytearray is not None:
                return self._bytearray[key]
            elif self._memoryview is not None:
                return self._memoryview[key]
            else:
                raise ValueError("Buffer is empty")


    # Allow the buffer to be set.
    def __setitem__(self, key:int, value:int) -> None:
        if self._bytes is not None:
            raise TypeError("Buffer is immutable")
        elif self._bytearray is not None:
            self._bytearray[key] = value
        elif self._memoryview is not None:
            self._memoryview[key] = value
        else:
            raise ValueError("Buffer is empty")
