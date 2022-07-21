import json
from types import FunctionType
from typing import Any

from helpers.voice import SoxFilter

# For whatever reason, the encoder seems to have a strange encoding order. 
# (Could be because `SoxFilter` is a dataclass? Not sure)

# Because of this, the "__sox_filter__" property may or may not exist 
# and is not a reliable method of searching for JSON objects encoding `SoxFilter`s.

# So, if the decoder finds "__sox_filter__", it'll assume the object is a `SoxFilter`.
# Otherwise, it'll assume it is a `SoxFilter` IFF both "fun" and "args" are present.

# Here's the TypeScript type for `SoxFilter`'s JSON object.
# interface SFObject {
#     "__sox_filter__"?: true,
#     "fun": string,
#     "args": { [s: string]: any }
# }

class SFEncoder(json.JSONEncoder):
    """
    Encodes `SoxFilter`s into JSON.
    
    Usage: `json.dump(data, fp, cls=BoardEncoder)`
    """
    def default(self, obj):
        if isinstance(obj, FunctionType):
            return obj.__name__
        if isinstance(obj, SoxFilter):
            return {
                "__sox_filter__": True,
                "fun": obj.fun.__name__,
                "args": obj.args
            }
        return json.JSONEncoder.default(self, obj)

def sf_from_json(o: "dict[str, Any]") -> "SoxFilter | dict[str, Any]":
    """
    Converts a JSON object into a `SoxFilter`.

    Usage: `json.load(fp, object_hook=sf_from_json)`
    """

    if o.get("__sox_filter__", None) is not None:
        return SoxFilter(o["fun"], o["args"].values())
    elif (
        (fun := o.get("fun", None)) is not None and
        (args := o.get("args", None)) is not None
        ):
        return SoxFilter(fun, args.values())
    return o
