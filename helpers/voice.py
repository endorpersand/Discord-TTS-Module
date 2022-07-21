import re
import typing
import discord
from discord.ext import commands

import dataclasses
import functools
import gtts.lang
import inspect
import requests
import sox

from collections.abc import Iterable
from gtts import gTTS
from pathlib import Path
from typing import Any, Callable, ClassVar, TypeVar

T = TypeVar("T")

CACHE_FOLDER = Path("_cache")
"""
Audio files have to be created to temporarily store sent messages. They are saved to this folder.
"""
CACHE_FOLDER.mkdir(exist_ok=True)

@dataclasses.dataclass
class Voice:
    """
    Dataclass holding information about a voice. This includes:
    - `lang` & `tld`: the language type and from which `translate.google.[tld]` (both determine voice accent)
    
    - `use_effects`: 
        - If `True`, use `effects` and ignore `pitch`.
        - If `False`, use `pitch` and ignore `effects`.
    - `pitch`: How pitch-shifted this voice is
    - `effects`: Sox filter effects applied onto this voice
    """

    lang: str
    tld: str
    pitch: float
    effects: "list[SoxFilter]"
    use_effects: bool

    valid_accents: "ClassVar[dict[tuple[str, str], tuple[str, ...]]]" = {
        # filter Chinese (Mandarin) in favor for Mandarin (*) below
        **{(k, "com"): (v,) for k, v in gtts.lang.tts_langs().items() if v not in ("Chinese (Mandarin)", )},

        # from documentation
        # some are removed if they match another pair's accent
        # tuple = (main name, *aliases)
        ("en", "com.au"): ("English (Australia)",),
        ("en", "co.uk"):  ("English (UK)",),
        ("en", "com"):    ("English (US)", "English"),
        ("en", "co.in"):  ("English (India)",),
        ("fr", "ca"):     ("French (Canada)",),
        ("fr", "com"):    ("French (France)", "French"),
        ("zh-CN", "com"): ("Mandarin (China Mainland)", "Mandarin"),
        ("zh-TW", "com"): ("Mandarin (Taiwan)",),
        ("pt", "com"):    ("Portuguese (Brazil)", "Portuguese"),
        ("pt", "pt"):     ("Portuguese (Portugal)",),
        ("es", "com"):    ("Spanish (Mexico)", "Spanish"),
        ("es", "es"):     ("Spanish (Spain)",),
    }
    """
    A mapping from `(lang, tld)` pair to that accent's aliases
    """

    # This request of TLDs from Google is given as a list of domains in the format: `.google.tld`
    # Thus, to get the valid *TLDs*, remove the `.google.`
    __resp = requests.get("https://www.google.com/supported_domains")
    allowed_tlds = [tld[8:] for tld in __resp.content.decode('utf-8').splitlines()]
    """
    All `tld`s accepted by Google Translate.
    """

    @classmethod
    def all_accent_aliases(cls):
        for name in cls.valid_accents.values():
            yield from name

    def __init__(
        self, 
        lang: str = "en", 
        tld: str = "com", 
        pitch: float = 0, 
        effects: "Iterable[SoxFilter] | None" = None,
        use_effects: bool = False
    ):
        langs = tuple(k for k, _ in self.valid_accents)
        if lang not in langs:
            raise ValueError(f"Invalid language {lang}")

        if tld not in self.allowed_tlds:
            raise ValueError(f"Invalid TLD {tld}")

        self.lang = lang
        self.tld = tld
        self.pitch = min(max(-12, pitch), 12)
        self.effects = list(effects) if effects is not None else ()
        self.use_effects = bool(use_effects)

    @classmethod
    def from_name(cls, name: str, pitch=0):
        """
        Parses a name into a voice with a matching `lang, tld` pair.

        A name can either be an alias or of the form `lang@tld`.
        """

        if "@" in name:
            lang, _, tld = name.partition("@")
            return cls(lang, tld, pitch)

        try:
            pair = next(p for p, n in cls.valid_accents.items() if 
                (isinstance(n, str) and name == n) or
                (isinstance(n, tuple) and name in n)
        )
        except StopIteration:
            raise ValueError("Invalid accent")
        
        return cls(*pair, pitch)

    def say(self, text: str):
        """
        Create an `AudibleText` object that can be read, using this Voice as the voice.
        """
        return AudibleText(text, self)
    
    @property
    def accent_name(self):
        """
        Get this voice's name.

        This is either the first in the list of aliases OR a placeholder text `??? (lang@tld)`.
        """
        acname = self.valid_accents.get((self.lang, self.tld), f"??? ({self.lang}@{self.tld})")
        
        if isinstance(acname, tuple): return acname[0]
        return acname
    
    @functools.cached_property
    def transformer(self) -> sox.Transformer:
        """
        Compute the `sox.Transformer` this voice has after applying all the effects.

        This function assumes this `Voice` was not modified ever (which it SHOULD not be).
        Use `copy` if you want to change a `Voice`.
        """
        # add effects & pitch
        # if effects are present (& enabled), pitch is ignored
        tfm = sox.Transformer()

        if self.use_effects:
            for f in self.effects:
                f.apply(tfm)
        elif self.pitch != 0:
            tfm.pitch(self.pitch)

        return tfm


    def copy(self, **kwargs):
        """
        Copy this `Voice` but modify some parameters.
        """
        dct = dataclasses.asdict(self)
        dct.update(kwargs)

        return Voice(**dct)

@dataclasses.dataclass
class SoxFilter:
    """
    A filter (using Sox) that can be applied to a voice
    """
    
    fun: Callable
    args: "dict[str, Any]"

    valid_filters = (
        "allpass", "bandpass", "bandreject", "bass", "bend", "biquad", "chorus", "compand", 
        "contrast", "dcshift", "deemph", "delay", "downsample", "echo", "echos", "equalizer", 
        "fade", "fir", "flanger", "gain", "highpass", "hilbert", "loudness", "lowpass", 
        "mcompand", "norm", "oops", "overdrive", "pad", "phaser", "pitch", "rate", "remix", 
        "repeat", "reverb", "reverse", "silence", "sinc", "speed", "swap", "tempo", "treble", 
        "tremolo", "trim", "upsample", "vad", "vol"
    )
    """
    A tuple of all supported Sox filters.
    """

    def __init__(self, fun: str, args: list):
        # ex: sox.Transformer.upsample(self, factor: int)
        self.fun: Callable = self.get_filter(fun)

        # verify the function fits the parameters
        # get the parameters (ignoring the self parameter)
        bound_args = inspect.signature(self.fun).bind(None, *args)
        bound_args.apply_defaults()
        self.args = dict(bound_args.arguments)
        self.args.pop("self", None)
    
    @classmethod
    def get_filter(cls, fil: str) -> Callable:
        """
        Get the function version of a filter given its name
        """
        if fil not in cls.valid_filters: raise ValueError(f"Unrecognized filter `{fil}`")

        return getattr(sox.Transformer, fil)

    def test(self):
        """
        Check if this filter is valid (i.e. does not have argument type errors)
        """
        self.apply(sox.Transformer())

    def apply(self, tf: sox.Transformer):
        """
        Apply filter to a given `Transformer`.
        """
        self.fun(tf, **self.args)
    
    def __str__(self):
        params = ", ".join(f"{p}={v!r}" for p, v in self.args.items())
        return f"{self.fun.__name__}({params})"

class AudibleText(gTTS):
    """
    Class that converts text into output audio
    """

    AUDIO_PATH1 = CACHE_FOLDER / 'gtts_out.mp3'
    AUDIO_PATH2 = CACHE_FOLDER / 'sox_out.mp3'

    def __init__(self, text, voice=Voice()):
        self.voice = voice
        super().__init__(text, lang=voice.lang, tld=voice.tld)
    
    def __repr__(self):
        return f"AudibleText({repr(self.text)}, voice={self.voice})"
    
    def build_audio(self):
        self.save(self.AUDIO_PATH1)
        self.voice.transformer.build(
            input_filepath=str(self.AUDIO_PATH1), 
            output_filepath=str(self.AUDIO_PATH2)
        )

    def play_in(self, vc: discord.VoiceClient, *, after: Callable = None):  # type: ignore
        self.build_audio()

        audio = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(str(self.AUDIO_PATH2)))
        vc.play(audio, after=after)


class ParseEffects:
    """
    Parser that reads arguments to `tts effects`.

    This class does not verify that arguments provided match the argument types of the sox filter;
    it only converts a string into a list of Python objects.

    However, the sox filter itself verifies the arguments match, so woohoo.

    The grammar of the parser in EBNF-inspired form:
    ```ebnf
    list  = "[" (unit),* "]"
    dict  = "{" (int ":" unit),* "}" # note that only dict[int, *] is valid
    bool  = "true" | "false"
    none  = "none"
    pair  = "(" float "," float ")"
    int   = ? anything parseable as an int ?
    float = ? anything parseable as a float ?
    str   = ? anything else ?

    unit  = ? any token above ?
    ```

    Usage: `[*ParseEffects(argument_string)]`
    """

    # You: Wait. Did you manually write a whole parser JUST to read effect arguments?
    # Me: Yes. And?

    # HACK: Pylance does not support sentinels (i.e. object()), so:
    class NoMatchType: pass
    NO_MATCH = NoMatchType()
    """
    Sentinel object designating that the next token could not be matched to the given type.
    """

    def __init__(self, arg_str: str):
        norm = "".join(c for c in arg_str.lower() if c.isascii and c.isprintable)

        # the list of tokens consists of "{", "}", "(", ")", "[", "]", ",", ":", 
        # and any words (miscellaneous text separated by spaces)

        # whitespace is ignored.

        self.tokens: "list[str]" = [
            t for s in re.findall(r"\S+", norm) for t in re.split(r"([{}()\[\],:])", s) if t != ""
        ]
        self.cursor = 0
    
    def peek(self):
        """
        Look forward to the next token (but do not advance the cursor).

        If the parser reaches the end of the argument list, return `None`.
        """
        if self.cursor >= len(self.tokens): return None
        return self.tokens[self.cursor]

    def next(self):
        """
        Look forward to the next token (and advance the cursor).

        If the parser reaches the end of the argument list, return `None`.
        """
        val = self.peek()
        self.cursor += 1
        return val

    def matches(self, string: str) -> bool:
        """
        Check if the next token matches a specified string.

        If it does, advance the cursor and return `True`. Otherwise, do nothing and return `False`.
        """
        hit = self.peek() == string
        if hit: self.cursor += 1

        return hit

    def err_at_cursor(self, msg: str):
        """
        Throw a `ValueError` with a given message at the current cursor point.
        """

        if self.cursor >= len(self.tokens):
            l, m = " ".join(self.tokens), "[EOL]"
            raise ValueError(f"{msg}:\n{l}**{m}**")
        else:
            l, m = " ".join(self.tokens[:self.cursor]), self.tokens[self.cursor]

            if self.cursor + 1 == len(self.tokens):
                raise ValueError(f"{msg}:\n{l}**{m}**")

            raise ValueError(f"{msg}:\n{l}**{m}**...")

    def require(self, match_fn: Callable[[], "T | NoMatchType"], expected: str = "") -> T:
        """
        Require that the next token is matched by the specified match function.

        Optionally provide a parameter to note in the error message what token was expected.
        """

        hit = match_fn()

        if isinstance(hit, self.NoMatchType):
            if len(expected) == 0: self.err_at_cursor("Unexpected token")
            self.err_at_cursor(f"Expected {expected} here")
        
        return hit

    def require_str(self, match_str: str):
        """
        Require that the next token matches a specified string.
        """
        hit = self.matches(match_str)

        if not hit:
            self.err_at_cursor(f"Expected `{match_str}` here")
        
        return match_str

    def match_trool(self):
        """
        Match the next token to `true`, `false`, or `none`.
        If the next token does not match, return `NO_MATCH`.
        """
        if self.matches("true"): return True
        elif self.matches("false"): return False
        elif self.matches("none"): return None
        return self.NO_MATCH

    def match_int(self):
        """
        Match the next token as a `int`.
        If the next token cannot be parsed as an `int`, return `NO_MATCH`.
        """
        try:
            v = int(self.peek()) # type: ignore
        except (ValueError, TypeError):
            return self.NO_MATCH
        
        self.next()
        return v
    
    def match_str(self):
        """
        Match the next token as an alphabetic string.
        If the next token cannot be parsed as an alphabetic string, return `NO_MATCH`.
        """
        pk = self.peek()
        if pk is not None and pk.isalpha():
            return self.next()
        return self.NO_MATCH

    def match_float(self):
        """
        Match the next token as a `float`.
        If the next token cannot be parsed as a `float`, return `NO_MATCH`.
        """
        try:
            v = float(self.peek()) # type: ignore
        except (ValueError, TypeError):
            return self.NO_MATCH
        
        self.next()
        return v

    def match_pair(self):
        """
        Match the next few tokens a pair of floats: `(float, float)`.

        If the next token is not `(`, these tokens do not represent a pair. Return `NO_MATCH`.
        If the parameters of the pairs are not floats, the parser will error.
        """
        if self.matches("("):
            f1 = self.require(self.match_float, expected="float")
            self.require_str(",")
            f2 = self.require(self.match_float, expected="float")
            self.require_str(")")

            return typing.cast("tuple[float, float]", (f1, f2))
        return self.NO_MATCH
    
    def match_list(self):
        """
        Match the next few tokens to a list.

        If the next token is not `[`, these tokens do not represent a list. Return `NO_MATCH`.
        """
        if self.matches("["):
            out = []

            unit = self.match_unit()
            if not isinstance(unit, self.NoMatchType): 
                out.append(unit)
                while self.matches(","):
                    unit = self.match_unit()
                    if not isinstance(unit, self.NoMatchType):
                        out.append(unit)
                    else:
                        break
            self.require_str("]")
            return out
        return self.NO_MATCH
    
    def match_entry(self):
        """
        Match the next few tokens to an entry. `int: unit`.

        This match function can return `NO_MATCH` if the next object is not an entry 
        or error if it is partially through parsing an entry and cannot recognize the tokens as an entry.
        """
        key = self.match_int()
        if not isinstance(key, self.NoMatchType):
            key = typing.cast(int, key)

            self.require_str(":")
            value = self.require(self.match_unit, expected="value")
            return (key, value)
        return self.NO_MATCH

    def match_dict(self):
        """
        Matches the next few tokens to a dict.
        """
        if self.matches("{"):
            out = {}

            entry = self.match_entry()
            if not isinstance(entry, self.NoMatchType):
                k, v = entry
                out[k] = v
                while self.matches(","):
                    entry = self.match_entry()
                    if not isinstance(entry, self.NoMatchType):
                        k, v = entry
                        out[k] = v
                    else:
                        break

            self.require_str("}")
            return out
        return self.NO_MATCH
    
    def match_unit(self):
        """
        Matches most types. If it cannot find any match using any of the match functions, 
        then return `NO_MATCH`.
        """
        match_fns = (
            self.match_trool,
            self.match_int,
            self.match_float,
            self.match_str,
            self.match_pair,
            self.match_list,
            self.match_dict
        )

        return next(
            (res for f in match_fns if not isinstance(res := f(), self.NoMatchType)),
            self.NO_MATCH
        )

    def __iter__(self):
        while self.peek() is not None:
            yield self.require(self.match_unit, expected="value")
