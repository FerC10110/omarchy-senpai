#!/usr/bin/env python3
"""
ani-py - standalone anime CLI

Single-file, standard-library-only Python application.
External tools are used where they make sense (curl/curl-impersonate,
fzf/rofi/dmenu, mpv/vlc/iina, yt-dlp/ffmpeg, ani-skip).

This is an independent Python implementation inspired by the workflow of
terminal anime launchers. Provider markup can change; scraping code is kept
isolated behind provider adapters for easier repair and failover.
"""
from __future__ import annotations

import argparse
import base64
import bisect
import dataclasses
import hashlib
import html
import http.server
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, NoReturn, Optional, Sequence, TypedDict
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlsplit

APP_NAME = "ani-py"
# Calendar version (CalVer): the date the most recent user-visible change landed.
# Monotonic by construction and comparable across automated release snapshots.
# Bump it in the same commit as the change - see "Versioning" in CONTRIBUTING.md.
VERSION = "2026.10.6"
# How many consecutive failed mpv IPC polls wait_for_completion tolerates before
# declaring the socket dead. mpv answers "property unavailable" for a few
# milliseconds after its socket appears but before the first file loads, so one
# failed poll must never be mistaken for a closed player. At the default 0.2s
# poll interval this is a ~5s grace period.
IPC_GRACE_POLLS = 25
# How long a stream may sit in mpv's cache-retry state before it is treated as
# stalled rather than slow. Long episodes never trip this: it only counts
# continuous paused-for-cache time.
IPC_STALL_SECONDS = 45
# One shared rejection message: --auto-next cannot work without a completion
# signal, and the user should learn that before a search, not after.
AUTO_NEXT_UNSUPPORTED = (
    "--auto-next requires desktop mpv with private IPC. "
    "VLC, IINA, custom players, Windows named-pipe IPC, and Android "
    "intent players do not expose a reliable natural-EOF signal to ani-py."
)
# Default ceiling on --auto-next queue length; 0 disables the ceiling.
AUTO_NEXT_DEFAULT_LIMIT = 12
BASE_URL = "https://hianime.at"
ANILIGHT_BASE_URL = "https://anilight.live"
ANILIGHT_API_URL = "https://api.anilight.live/api"
# External subtitle lookup: MAL id -> AniDB episode id -> release attachments.
ANIZIP_URL = "https://api.ani.zip/mappings"
ANIMETOSHO_FEED_URL = "https://feed.animetosho.org/json"
ANIMETOSHO_ATTACH_URL = "https://animetosho.org/storage/attach"
SUBTITLE_MAX_BYTES = 5 * 1024 * 1024
SUBTITLE_MAX_RELEASES = 6
# Two subtitles of one episode start their cues together. Measured on real
# tracks: same episode 0.86-1.00 of cue starts within the tolerance, a
# different episode 0.14-0.21.
SUBTITLE_SYNC_TOLERANCE = 0.35
SUBTITLE_SYNC_MINIMUM = 0.5
# With fewer cues than this, half of them landing by chance is not rare enough.
SUBTITLE_SYNC_MIN_CUES = 20
# A found track that speaks in under this share of the stretches the provider
# track speaks in translates signs and songs only, however well those few cues
# line up. Measured on real tracks: full translations 0.78-1.39, a forced
# track 0.04.
SUBTITLE_MIN_COVERAGE = 0.5
SUBTITLE_COVERAGE_SLOT = 2.0
ANIMEAV1_BASE_URL = "https://animeav1.com"
ANIMEFLV_BASE_URL = "https://animeflv.or.at"
MP4UPLOAD_REFERER = "https://www.mp4upload.com/"
# Runtime updates are published as versioned GitHub Release assets after the
# main-branch test workflow passes. The moving "latest" pointer selects a release,
# while SHA256SUMS verifies that the downloaded standalone script belongs to it.
RELEASE_BASE_URL = "https://github.com/Onehand-Coding/ani-py/releases/latest/download"
UPDATE_URL = f"{RELEASE_BASE_URL}/ani-py"
UPDATE_CHECKSUM_URL = f"{RELEASE_BASE_URL}/SHA256SUMS"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
XOR_KEY = b"otaku-embed-v1"


# ---------- terminal / style ----------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def color_enabled() -> bool:
    return sys.stderr.isatty() and os.getenv("NO_COLOR") is None


def sty(text: str, *codes: str) -> str:
    if not color_enabled():
        return text
    return "".join(codes) + text + C.RESET


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(value: str) -> str:
    """Remove ANSI color codes (fzf --ansi strips them from its output)."""
    return _ANSI_RE.sub("", value)


def term_width(default: int = 88) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


_JSON_OUTPUT = False


def set_json_output(enabled: bool) -> None:
    """--json: stdout carries JSON lines only; everything human goes to stderr."""
    global _JSON_OUTPUT
    _JSON_OUTPUT = enabled


def json_output() -> bool:
    return _JSON_OUTPUT


def emit(payload: dict) -> None:
    """One JSON object per line; the only thing stdout carries in --json mode."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def say(text: str = "") -> None:
    """Human-facing text that normally belongs on stdout."""
    print(text, file=sys.stderr if _JSON_OUTPUT else sys.stdout)


def clear_screen() -> None:
    if sys.stdout.isatty() and not _JSON_OUTPUT:
        print("\033[2J\033[H", end="")


def banner(subtitle: Optional[str] = None) -> None:
    width = min(term_width(), 96)
    title = f" {APP_NAME} "
    left = max(1, (width - len(title)) // 2)
    right = max(1, width - left - len(title))
    say(sty("━" * left, C.DIM, C.CYAN) + sty(title, C.BOLD, C.CYAN) + sty("━" * right, C.DIM, C.CYAN))
    if subtitle:
        say(sty(subtitle, C.DIM))


def status(message: str) -> None:
    print(f"{sty('●', C.CYAN)} {message}", file=sys.stderr)


def ok(message: str) -> None:
    print(f"{sty('✓', C.GREEN)} {message}", file=sys.stderr)


def warn(message: str) -> None:
    print(f"{sty('!', C.YELLOW)} {message}", file=sys.stderr)


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"{sty('error', C.BOLD, C.RED)}  {message}", file=sys.stderr)
    if _JSON_OUTPUT:
        emit({"error": message})
    raise SystemExit(code)


def clock(seconds: float) -> str:
    """m:ss, widening to h:mm:ss only when needed."""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class NowPlaying:
    """Live "now playing" display for unattended playback.

    Auto-next hands the terminal back to the user for as long as an episode
    runs, and the banner printed at the start of an episode is never seen again.
    This redraws one line in place so the current episode, its position and what
    comes next stay visible, and mirrors a short form into the terminal tab
    title so the state is readable when the terminal is not focused.

    Both go to stderr, where the rest of the progress output goes, so ordering
    survives redirection. Everything is suppressed when stderr is not a
    terminal: piped output and log files must not gain carriage returns or
    escape sequences.
    """

    def __init__(self) -> None:
        self.enabled = sys.stderr.isatty()
        self._drawn = False
        self._last = 0.0

    def title(self, text: str) -> None:
        if not self.enabled:
            return
        print(f"\033]2;{text}\007", end="", file=sys.stderr, flush=True)

    def update(self, line: str, interval: float = 0.5) -> None:
        """Redraw the status line, at most every `interval` seconds."""
        if not self.enabled:
            return
        now = time.monotonic()
        if self._drawn and now - self._last < interval:
            return
        self._last = now
        self._drawn = True
        print("\r\033[K" + line, end="", file=sys.stderr, flush=True)

    def clear(self) -> None:
        if not self.enabled or not self._drawn:
            return
        self._drawn = False
        print("\r\033[K", end="", file=sys.stderr, flush=True)


# ---------- models / provider contracts ----------

@dataclasses.dataclass(frozen=True)
class Anime:
    provider_id: str
    title: str
    provider: str = "hianime"

    @property
    def slug(self) -> str:
        """Backward-compatible alias for older callers/tests."""
        return self.provider_id


@dataclasses.dataclass(frozen=True)
class Episode:
    episode_id: str
    number: str


@dataclasses.dataclass(frozen=True)
class Stream:
    quality: str
    url: str


@dataclasses.dataclass(frozen=True)
class SubtitleTrack:
    url: str
    language: Optional[str] = None
    label: Optional[str] = None
    default: bool = False
    # Set for a file an external SubtitleSource found; `url` is then a local path.
    source: Optional[str] = None

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        if self.language:
            return self.language
        return "Subtitle"


@dataclasses.dataclass
class StreamBundle:
    streams: list[Stream]
    subtitle: Optional[str]
    referer: str
    mal_id: Optional[str]
    provider: str = "unknown"
    subtitle_language: Optional[str] = None
    subtitle_label: Optional[str] = None
    subtitles: list[SubtitleTrack] = dataclasses.field(default_factory=list)

    def subtitle_tracks(self) -> list[SubtitleTrack]:
        tracks = list(self.subtitles)
        if self.subtitle and not any(track.url == self.subtitle for track in tracks):
            tracks.insert(
                0,
                SubtitleTrack(
                    url=self.subtitle,
                    language=self.subtitle_language,
                    label=self.subtitle_label,
                    default=True,
                ),
            )
        return tracks


@dataclasses.dataclass
class HistoryEntry:
    episode: str
    provider: str
    provider_id: str
    title: str
    # Whether that episode was watched to the end. Rows written before this
    # field existed have no such information and are read as completed, which
    # reproduces the behaviour those rows were written under: `--continue`
    # advances to the next episode. Defaulting them the other way would make
    # an already-finished episode replay from its first frame.
    completed: bool = True

    @property
    def anime_slug(self) -> str:
        return self.provider_id


@dataclasses.dataclass
class DetachedSession:
    socket: str
    player: str
    provider: str
    provider_id: str
    title: str
    episode: str
    quality: str
    mode: str
    source_provider: str
    subtitle_preference: str = "auto"


@dataclasses.dataclass(frozen=True)
class ProviderCapabilities:
    sub: bool = True
    dub: bool = False
    subtitles: bool = False
    qualities: bool = True
    mal_id: bool = False


class AniPyError(RuntimeError):
    pass


class HttpError(AniPyError):
    pass


class ProviderError(AniPyError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class AnimeNotFound(ProviderError):
    pass


class EpisodeNotFound(ProviderError):
    pass


class StreamNotFound(ProviderError):
    pass


class ProviderChanged(ProviderError):
    pass


class Provider:
    name = "unknown"
    display_name = "Unknown"
    capabilities = ProviderCapabilities()
    experimental = False

    def available(self) -> bool:
        """Preflight gate: False means automatic paths must skip this provider."""
        return True

    def search(self, query: str) -> list[Anime]:
        raise NotImplementedError

    def episodes(self, anime: Anime | str) -> list[Episode]:
        raise NotImplementedError

    def resolve(self, anime: Anime | str, episode: Episode, mode: str) -> StreamBundle:
        raise NotImplementedError


# ---------- process helpers ----------

def which_first(candidates: Iterable[str]) -> Optional[str]:
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        p = Path(candidate).expanduser()
        if p.exists():
            return str(p)
    return None


def split_flags(value: str) -> list[str]:
    return shlex.split(value) if value.strip() else []


def run_capture(cmd: Sequence[str], *, input_text: Optional[str] = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


# ---------- networking ----------

class HttpClient:
    """curl-backed HTTP client with no third-party Python dependencies."""

    def __init__(self) -> None:
        override = os.getenv("ANI_PY_CURL")
        names = [override] if override else [
            "curl_firefox135",
            "curl_chrome136",
            "curl_chrome116",
            "curl_ff117",
            "curl",
        ]
        self.exe = which_first([x for x in names if x])
        if not self.exe:
            fail("curl was not found. Install curl or a curl-impersonate binary.")

    def request(
        self,
        method: str,
        url: str,
        *,
        referer: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: int = 15,
        json_body: Optional[object] = None,
        cookie_jar: Optional[str] = None,
    ) -> str:
        marker = "__ANI_PY_HTTP__"
        cmd = [
            self.exe,
            "-sS",
            "-L",
            "--max-time",
            str(timeout),
            "-A",
            USER_AGENT,
            "-w",
            f"\\n{marker}%{{http_code}}",
        ]
        if method.upper() != "GET":
            cmd += ["-X", method.upper()]
        if referer:
            cmd += ["-e", referer]
        for key, value in (headers or {}).items():
            cmd += ["-H", f"{key}: {value}"]
        if cookie_jar:
            cmd += ["-b", cookie_jar, "-c", cookie_jar]
        if json_body is not None:
            cmd += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(json_body)]
        cmd.append(url)

        proc = run_capture(cmd)
        output = proc.stdout
        if marker not in output:
            detail = proc.stderr.strip() or f"curl exit {proc.returncode}"
            raise HttpError(f"Network request failed for {url}: {detail}")

        body, _, status_text = output.rpartition("\n" + marker)
        try:
            http_status = int(status_text.strip())
        except ValueError as exc:
            raise HttpError(f"Invalid HTTP response while requesting {url}") from exc

        if proc.returncode != 0:
            detail = proc.stderr.strip() or f"curl exit {proc.returncode}"
            raise HttpError(f"Network request failed for {url}: {detail}")

        if not (200 <= http_status < 300):
            challenge = " browser challenge" if "Just a moment" in body else ""
            raise HttpError(f"HTTP {http_status}{challenge} from {url}")
        return body

    def get(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: int = 15,
        cookie_jar: Optional[str] = None,
    ) -> str:
        return self.request(
            "GET", url, referer=referer, headers=headers, timeout=timeout, cookie_jar=cookie_jar
        )

    def get_bytes(self, url: str, *, timeout: int = 30) -> bytes:
        """Fetch a binary payload without locale decoding or newline translation."""
        cmd = [
            self.exe,
            "-fsSL",
            "--max-time",
            str(timeout),
            "-A",
            USER_AGENT,
            url,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip() or f"curl exit {proc.returncode}"
            raise HttpError(f"Network request failed for {url}: {detail}")
        return proc.stdout

    def get_json(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: int = 15,
        cookie_jar: Optional[str] = None,
    ) -> object:
        # Explicit keywords rather than **kwargs: an object-typed catch-all
        # makes every forwarded argument uncheckable at the call site.
        body = self.get(
            url, referer=referer, headers=headers, timeout=timeout, cookie_jar=cookie_jar
        )
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise HttpError(f"Expected JSON from {url}") from exc

    def post_json(
        self,
        url: str,
        payload: object,
        *,
        referer: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: int = 15,
        cookie_jar: Optional[str] = None,
    ) -> object:
        body = self.request(
            "POST",
            url,
            json_body=payload,
            referer=referer,
            headers=headers,
            timeout=timeout,
            cookie_jar=cookie_jar,
        )
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise HttpError(f"Expected JSON from {url}") from exc


# ---------- provider helpers ----------

def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pat = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")
    for match in pat.finditer(raw):
        out[match.group(1).lower()] = next((x for x in match.groups()[1:] if x is not None), "")
    return out


def _plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def _provider_id(anime: Anime | str) -> str:
    return anime.provider_id if isinstance(anime, Anime) else anime


def _normalize_title(value: str) -> str:
    value = html.unescape(value).casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_LATIN_SPANISH = "es-419"
_LATIN_SPANISH_ALIASES = frozenset({
    "latino", "latam", "es-419", "es-la", "es-mx", "spa-la",
    "español latino", "espanol latino",
})
_SPANISH_WORD_RE = re.compile(r"spanish|espa[ñn]ol|castellano")
_SUBTITLE_LANGUAGE_ALIASES = {
    "english": "en",
    "eng": "en",
    "en-us": "en",
    "en-gb": "en",
    "japanese": "ja",
    "jpn": "ja",
    "spanish": "es",
    "spa": "es",
    "español": "es",
    "espanol": "es",
    "castellano": "es",
    "es-es": "es",
    "french": "fr",
    "fre": "fr",
    "fra": "fr",
    "german": "de",
    "ger": "de",
    "deu": "de",
    "portuguese": "pt",
    "por": "pt",
    "italian": "it",
    "ita": "it",
    "russian": "ru",
    "rus": "ru",
    "arabic": "ar",
    "ara": "ar",
    "indonesian": "id",
    "ind": "id",
    "korean": "ko",
    "kor": "ko",
    "chinese": "zh",
    "chi": "zh",
    "zho": "zh",
    "vietnamese": "vi",
    "vie": "vi",
    "thai": "th",
    "tha": "th",
    "polish": "pl",
    "pol": "pl",
    "turkish": "tr",
    "tur": "tr",
    "dutch": "nl",
    "dut": "nl",
    "nld": "nl",
    "hindi": "hi",
    "hin": "hi",
    "malay": "ms",
    "may": "ms",
    "msa": "ms",
}
# A request for one of these is matched as a language, never as a label fragment.
_KNOWN_SUBTITLE_LANGUAGES = frozenset(_SUBTITLE_LANGUAGE_ALIASES.values()) | {_LATIN_SPANISH}


def normalize_subtitle_language(value: object) -> Optional[str]:
    """Map a provider language code, a track label, or a user request to a code.

    Latin American Spanish is the one regional variant kept apart ("es-419"):
    providers name it only in the label, and someone asking for it does not
    want the European track that shares its base language.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace("_", "-")
    if not text:
        return None
    if text in _LATIN_SPANISH_ALIASES or ("latin" in text and _SPANISH_WORD_RE.search(text)):
        return _LATIN_SPANISH
    if text in _SUBTITLE_LANGUAGE_ALIASES:
        return _SUBTITLE_LANGUAGE_ALIASES[text]
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", text):
        return text.split("-", 1)[0]
    return None


def _label_subtitle_language(label: object) -> Optional[str]:
    """Language of a track label, read from its first word when the whole label is not one.

    "English (US)" and "Portuguese (- Portuguese(Brazil))" carry a region that
    is not a language code on its own.
    """
    language = normalize_subtitle_language(label)
    if language or not isinstance(label, str):
        return language
    word = re.match(r"[^\W\d_]+", label.strip().lower())
    return _SUBTITLE_LANGUAGE_ALIASES.get(word.group(0)) if word else None


def _acceptable_subtitle_languages(wanted: str) -> list[str]:
    """Languages that satisfy a request, best first.

    Plain Spanish takes the Latin American track as a second choice; a request
    for the Latin variant never takes the European one.
    """
    return ["es", _LATIN_SPANISH] if wanted == "es" else [wanted]


def _subtitle_language_matches(
    wanted: str, tracks: Sequence[SubtitleTrack]
) -> Optional[SubtitleTrack]:
    for language in _acceptable_subtitle_languages(wanted):
        hit = next((track for track in tracks if track.language == language), None)
        if hit:
            return hit
    return None


def _episode_number(value: object) -> Optional[str]:
    """Episode number as provider JSON gives it (int, float, or text), as text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return value[:-2] if value.endswith(".0") else value
    return None


def _sveltekit_value(flat: list, index: object, seen: dict[int, object]) -> object:
    if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < len(flat)):
        return None
    if index in seen:
        return seen[index]
    raw = flat[index]
    if isinstance(raw, dict):
        fields: dict[str, object] = {}
        seen[index] = fields
        for key, ref in raw.items():
            fields[key] = _sveltekit_value(flat, ref, seen)
        return fields
    if isinstance(raw, list):
        items: list[object] = []
        seen[index] = items
        for ref in raw:
            items.append(_sveltekit_value(flat, ref, seen))
        return items
    seen[index] = raw
    return raw


def _sveltekit_data(payload: object) -> dict[str, object]:
    """Merge the ``data`` nodes of a SvelteKit ``__data.json`` response.

    Each node is one flat array in which objects and lists hold indexes into
    that same array rather than values, with index 0 as the root. Negative
    indexes are SvelteKit's markers for undefined and friends; they read as
    None.
    """
    merged: dict[str, object] = {}
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict) or node.get("type") != "data":
            continue
        flat = node.get("data")
        if not isinstance(flat, list) or not flat:
            continue
        root = _sveltekit_value(flat, 0, {})
        if isinstance(root, dict):
            merged.update(root)
    return merged


def _resolve_mp4upload(http: HttpClient, embed_url: str, page_referer: str) -> str:
    """Direct MP4 URL from an MP4Upload embed page.

    The player script names the file in plain text, and the file host then
    only asks for MP4Upload's own Referer. The embed URL comes out of a
    scraped page, so it is fetched only when it really points at MP4Upload.
    """
    parts = urlsplit(embed_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not (host == "mp4upload.com" or host.endswith(".mp4upload.com")):
        raise ProviderChanged(f"Unexpected MP4Upload embed address: {embed_url!r}")
    try:
        page = http.get(embed_url, referer=page_referer)
    except HttpError as exc:
        raise ProviderUnavailable(f"MP4Upload embed failed: {exc}") from exc
    match = re.search(r'src:\s*"(https?://[^"]+\.mp4)"', page)
    if not match:
        raise ProviderChanged("MP4Upload player markup changed; no video source was present.")
    return match.group(1)


# ---------- AES-256-CBC decryption (stdlib only) ----------
#
# HiAnime's current embed backend ships the HLS URL inside a single
# AES-256-CBC blob. The key and IV are constants in the site's own player
# script, so the cipher is fully specified and only decryption is needed.
# Written out here because the standard library has no AES.


def _gmul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8) modulo the AES polynomial."""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        b >>= 1
        a = ((a << 1) ^ (0x1B if a & 0x80 else 0)) & 0xFF
    return result


def _build_sbox() -> list[int]:
    """Generate the AES S-box: multiplicative inverse plus affine transform."""
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        value = q
        for shift in (1, 2, 3, 4):
            value ^= ((q << shift) | (q >> (8 - shift))) & 0xFF
        sbox[p] = value ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_AES_SBOX = _build_sbox()
_AES_INV_SBOX = [0] * 256
for _index, _value in enumerate(_AES_SBOX):
    _AES_INV_SBOX[_value] = _index


def _aes256_key_schedule(key: bytes) -> tuple[list[list[int]], int]:
    words = [list(key[4 * i:4 * i + 4]) for i in range(8)]
    rcon = 1
    for i in range(8, 4 * (14 + 1)):
        temp = list(words[i - 1])
        if i % 8 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _gmul(rcon, 2)
        elif i % 8 == 4:
            temp = [_AES_SBOX[b] for b in temp]
        words.append([words[i - 8][j] ^ temp[j] for j in range(4)])
    return words, 14


def _add_round_key(state: list[int], words: list[list[int]], rnd: int) -> None:
    for i in range(16):
        state[i] ^= words[rnd * 4 + i // 4][i % 4]


def _inv_shift_rows(state: list[int]) -> list[int]:
    # Row r of the AES state is strided by 4 in this flat (r + 4c) layout, so
    # the inverse of a visual right-shift by r reads as an index shift of -r.
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = state[r + 4 * ((c - r) % 4)]
    return out


def _inv_mix_columns(state: list[int]) -> list[int]:
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = (state[r + 4 * c] for r in range(4))
        out[0 + 4 * c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        out[1 + 4 * c] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        out[2 + 4 * c] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        out[3 + 4 * c] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    return out


def _aes256_decrypt_block(block: bytes, words: list[list[int]], rounds: int) -> bytes:
    # Inverse cipher: the last round key comes off first, then each round
    # undoes MixColumns/ShiftRows/SubBytes in the reverse of the order the
    # forward cipher applied them.
    state = list(block)
    _add_round_key(state, words, rounds)
    state = _inv_shift_rows(state)
    state = [_AES_INV_SBOX[b] for b in state]
    for rnd in range(rounds - 1, 0, -1):
        _add_round_key(state, words, rnd)
        state = _inv_mix_columns(state)
        state = _inv_shift_rows(state)
        state = [_AES_INV_SBOX[b] for b in state]
    _add_round_key(state, words, 0)
    return bytes(state)


# Seed and IV the megaplay.buzz player uses for its source manifest.
MEGAPLAY_KEY = b"i?LMTAx0Q6,:}50U"
MEGAPLAY_IV = b"W0;27ToaUpl_P%'c"


def _aes256_cbc_decrypt(blob: str, key: bytes, iv: bytes) -> bytes:
    """Decrypt a base64url AES-256-CBC blob with the given 32-byte key and 16-byte IV."""
    if len(key) > 32:
        raise ValueError("AES-256 key must be at most 32 bytes")
    padded = key + b"\x00" * (32 - len(key))
    words, rounds = _aes256_key_schedule(padded)
    previous = (iv + b"\x00" * 16)[:16]
    raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
    out = bytearray()
    for start in range(0, len(raw) - 15, 16):
        block = raw[start:start + 16]
        plain = _aes256_decrypt_block(block, words, rounds)
        out += bytes(a ^ b for a, b in zip(plain, previous))
        previous = block
    if out:
        pad = out[-1]
        if 1 <= pad <= 16:
            out = out[:-pad]
    return bytes(out)


# ---------- HiAnime provider ----------

class HianimeProvider(Provider):
    name = "hianime"
    display_name = "HiAnime"
    capabilities = ProviderCapabilities(sub=True, dub=True, subtitles=True, qualities=True, mal_id=True)

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @staticmethod
    def _clean_title(value: str) -> str:
        return html.unescape(re.sub(r"\s+", " ", value)).strip()

    def search(self, query: str) -> list[Anime]:
        try:
            page = self.http.get(f"{BASE_URL}/search?keyword={quote_plus(query)}")
        except HttpError as exc:
            raise ProviderUnavailable(f"HiAnime search failed: {exc}") from exc
        if "<title>Just a moment" in page:
            raise ProviderUnavailable("HiAnime is behind a browser challenge; curl-impersonate may help.")

        page = page.split('id="main-sidebar"', 1)[0]
        found: list[Anime] = []
        seen: set[str] = set()
        pat = re.compile(
            r'<h3\s+class="film-name"[^>]*>.*?'
            r'<a\s+href="[^"]*/([^"/?#]+)"[^>]*\btitle="([^"]+)"',
            re.IGNORECASE | re.DOTALL,
        )
        for slug, title in pat.findall(page):
            title = self._clean_title(title)
            if slug not in seen:
                found.append(Anime(provider_id=slug, title=title, provider=self.name))
                seen.add(slug)
        return found

    def episodes(self, anime: Anime | str) -> list[Episode]:
        anime_slug = _provider_id(anime)
        numeric_id = anime_slug.rsplit("-", 1)[-1]
        try:
            page = self.http.get(f"{BASE_URL}/api/theme/episode/list/{numeric_id}")
        except HttpError as exc:
            raise ProviderUnavailable(f"HiAnime episode list failed: {exc}") from exc
        page = page.replace("\\", "")
        pat = re.compile(
            r'data-number="([^"]+)"[^>]*data-id="(\d+)".*?'
            r'/watch/' + re.escape(anime_slug) + r'\?ep=',
            re.IGNORECASE | re.DOTALL,
        )
        episodes = [Episode(episode_id=eid, number=num) for num, eid in pat.findall(page)]
        if not episodes:
            raise EpisodeNotFound(f"HiAnime returned no episodes for {anime_slug}.")
        return episodes

    @staticmethod
    def _decode_embed_hash(encoded: str) -> Optional[str]:
        if not encoded:
            return None
        try:
            return base64.b64decode(encoded).decode("utf-8", "replace")
        except Exception:
            return None

    @staticmethod
    def _deobfuscate(blob: str) -> dict:
        raw = base64.b64decode(blob)
        decoded = bytes(value ^ XOR_KEY[i % len(XOR_KEY)] for i, value in enumerate(raw))
        try:
            return json.loads(decoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("provider payload could not be decoded") from exc

    @staticmethod
    def _pick_source_url(payload: object) -> Optional[str]:
        if isinstance(payload, dict):
            src = payload.get("src")
            if isinstance(src, str) and ".m3u8" in src:
                return src
            for value in payload.values():
                hit = HianimeProvider._pick_source_url(value)
                if hit:
                    return hit
        elif isinstance(payload, list):
            for value in payload:
                hit = HianimeProvider._pick_source_url(value)
                if hit:
                    return hit
        return None

    @staticmethod
    def _subtitle_language(value: object) -> Optional[str]:
        return normalize_subtitle_language(value)

    @staticmethod
    def _subtitle_tracks(payload: object) -> list[SubtitleTrack]:
        tracks: list[SubtitleTrack] = []
        seen: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                subtitles = value.get("subtitles")
                if isinstance(subtitles, list):
                    for item in subtitles:
                        if not isinstance(item, dict):
                            continue
                        src = item.get("src") or item.get("file") or item.get("url")
                        if not isinstance(src, str) or not src or src in seen:
                            continue
                        label_obj = item.get("label") or item.get("name")
                        label = label_obj.strip() if isinstance(label_obj, str) and label_obj.strip() else None
                        language = None
                        for key in ("language", "lang", "srclang"):
                            language = HianimeProvider._subtitle_language(item.get(key))
                            if language:
                                break
                        if not language:
                            language = _label_subtitle_language(label)
                        if language == "es" and normalize_subtitle_language(label) == _LATIN_SPANISH:
                            language = _LATIN_SPANISH
                        tracks.append(
                            SubtitleTrack(
                                url=src,
                                language=language,
                                label=label,
                                default=bool(item.get("default")),
                            )
                        )
                        seen.add(src)
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        return tracks

    @staticmethod
    def _default_subtitle(tracks: Sequence[SubtitleTrack]) -> Optional[SubtitleTrack]:
        if not tracks:
            return None
        return next((track for track in tracks if track.default), None) or next(
            (track for track in tracks if track.language == "en"), None
        ) or tracks[0]

    @staticmethod
    def _pick_subtitle_info(payload: object) -> tuple[Optional[str], Optional[str], Optional[str]]:
        chosen = HianimeProvider._default_subtitle(HianimeProvider._subtitle_tracks(payload))
        if chosen is None:
            return None, None, None
        return chosen.url, chosen.language, chosen.label

    @staticmethod
    def _pick_subtitle(payload: object) -> Optional[str]:
        return HianimeProvider._pick_subtitle_info(payload)[0]

    def _resolve_megaplay(
        self, embed_page: str, referer: str, mode: str, mal_id: Optional[str]
    ) -> StreamBundle:
        """Resolve the megaplay.buzz backend, which serves an AES-encrypted
        source manifest from /stream/getSources instead of a window.__P blob."""
        # data-id is the per-episode, per-mode identifier the player sends to
        # getSources. data-realid is NOT equivalent: it is shared between the
        # sub and dub embeds, and asking for it returns a different show.
        id_match = re.search(r'data-id="(\d+)"', embed_page)
        if not id_match:
            raise ProviderChanged("HiAnime player markup changed; no media id was present.")
        media_id = id_match.group(1)
        try:
            payload = self.http.get(
                f"{referer}stream/getSources?id={media_id}&type={mode}",
                referer=referer,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        except HttpError as exc:
            raise ProviderUnavailable(f"HiAnime source manifest failed: {exc}") from exc
        try:
            sources = json.loads(payload)
            enc = sources["enc"]
            if not isinstance(enc, str):
                raise TypeError("enc is not a string")
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderChanged(f"HiAnime source manifest is malformed: {exc}") from exc
        try:
            manifest = json.loads(_aes256_cbc_decrypt(enc, MEGAPLAY_KEY, MEGAPLAY_IV))
        except (ValueError, TypeError) as exc:
            raise ProviderChanged(f"HiAnime source manifest could not be decrypted: {exc}") from exc
        master_url = manifest.get("file") if isinstance(manifest, dict) else None
        if not isinstance(master_url, str) or ".m3u8" not in master_url:
            raise StreamNotFound("HiAnime source manifest contained no HLS playlist.")
        tracks = self._subtitle_tracks({"subtitles": sources.get("tracks")})
        default_track = self._default_subtitle(tracks)
        try:
            master = self.http.get(master_url, referer=referer)
        except HttpError as exc:
            raise ProviderUnavailable(f"HiAnime HLS host failed: {exc}") from exc
        streams = self._parse_master(master, master_url)
        if not streams:
            streams = [Stream(quality="auto", url=master_url)]
        return StreamBundle(
            streams=streams,
            subtitle=default_track.url if default_track else None,
            referer=referer,
            mal_id=mal_id,
            provider=self.name,
            subtitle_language=default_track.language if default_track else None,
            subtitle_label=default_track.label if default_track else None,
            subtitles=tracks,
        )

    @staticmethod
    def _parse_master(master: str, master_url: str) -> list[Stream]:
        lines = [line.strip() for line in master.splitlines() if line.strip()]
        streams: list[Stream] = []
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF") or i + 1 >= len(lines):
                continue
            url = lines[i + 1]
            if url.startswith("#"):
                continue
            resolution = re.search(r"RESOLUTION=\d+x(\d+)", line, re.IGNORECASE)
            bandwidth = re.search(r"BANDWIDTH=(\d+)", line, re.IGNORECASE)
            quality = resolution.group(1) + "p" if resolution else (
                f"{int(bandwidth.group(1)) // 1000}k" if bandwidth else "auto"
            )
            streams.append(Stream(quality=quality, url=urljoin(master_url, url)))
        streams.sort(key=stream_rank, reverse=True)
        return streams

    def resolve(self, anime: Anime | str, episode: Episode, mode: str) -> StreamBundle:
        anime_slug = _provider_id(anime)
        try:
            server_page = self.http.get(
                f"{BASE_URL}/api/theme/episode/servers?episodeId={quote_plus(episode.episode_id)}"
            ).replace('\\"', '"')
        except HttpError as exc:
            raise ProviderUnavailable(f"HiAnime server lookup failed: {exc}") from exc

        embeds: list[tuple[str, str]] = []
        for server_name, encoded in self._server_hashes(server_page, mode):
            decoded = self._decode_embed_hash(encoded)
            if decoded:
                embeds.append((server_name, decoded))
        if not embeds:
            raise StreamNotFound(f"HiAnime has no {mode} source for episode {episode.number}.")

        # megaplay embeds carry no MAL id. ZokoAnime's URL does, and it is
        # readable from the server list even while that server is down, so
        # --skip and subtitle lookup keep working when playback falls through.
        listed_mal_id = self._listed_mal_id(url for _, url in embeds)

        # The embed hosts gate on Referer and answer HTTP 200 with their own
        # error page when it is missing, so the watch page is sent as one.
        watch_referer = f"{BASE_URL}/watch/{anime_slug}?ep={episode.number}"

        # ZokoAnime is tried first; if its embed or HLS host is broken, fall
        # through to the other servers HiAnime lists for the same episode.
        failures: list[tuple[str, ProviderError]] = []
        for index, (server_name, embed_url) in enumerate(embeds):
            try:
                bundle = self._resolve_embed(embed_url, mode, watch_referer)
            except ProviderError as exc:
                failures.append((server_name, exc))
                if index + 1 < len(embeds):
                    warn(f"HiAnime {server_name} server failed: {exc}; trying {embeds[index + 1][0]}.")
                continue
            if not bundle.mal_id:
                bundle.mal_id = listed_mal_id
            return bundle
        first = failures[0][1]
        if len(failures) == 1:
            raise first
        others = ", ".join(name for name, _ in failures[1:])
        raise type(first)(f"{first} (other servers also failed: {others})") from first

    @staticmethod
    def _listed_mal_id(embed_urls: Iterable[str]) -> Optional[str]:
        for url in embed_urls:
            match = re.search(r"/mal/(\d+)/", url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _server_hashes(server_page: str, mode: str) -> list[tuple[str, str]]:
        """Return (server name, embed hash) pairs for ``mode``, ZokoAnime first."""
        found: list[tuple[str, str]] = []
        for tag in re.findall(r'<[^>]*class="[^"]*server-item[^"]*"[^>]*>', server_page, re.I):
            attrs = _attrs(tag)
            name = attrs.get("data-server-name", "")
            encoded = attrs.get("data-hash")
            if attrs.get("data-type") == mode and name and encoded and (name, encoded) not in found:
                found.append((name, encoded))
        if not any(name.lower() == "zokoanime" for name, _ in found):
            m = re.search(
                rf'data-type="{re.escape(mode)}".*?data-server-name="ZokoAnime".*?data-hash="([^"]+)"',
                server_page,
                re.I | re.S,
            )
            if m:
                found.append(("ZokoAnime", m.group(1)))
        found.sort(key=lambda item: item[0].lower() != "zokoanime")
        return found

    def _resolve_embed(self, embed_url: str, mode: str, page_referer: Optional[str] = None) -> StreamBundle:
        parts = urlsplit(embed_url)
        referer = f"{parts.scheme}://{parts.netloc}/"
        mal_match = re.search(r"/mal/(\d+)/", embed_url)
        mal_id = mal_match.group(1) if mal_match else None
        try:
            embed_page = self.http.get(embed_url, referer=page_referer or referer)
        except HttpError as exc:
            raise ProviderUnavailable(f"HiAnime embed host failed: {exc}") from exc
        if 'class="error-container"' in embed_page:
            raise ProviderUnavailable(
                f"HiAnime embed host {parts.netloc} served its error page instead of a player"
            )
        blob_match = re.search(r'window\.__P\s*=\s*"([^"]+)"', embed_page)
        if not blob_match:
            return self._resolve_megaplay(embed_page, referer, mode, mal_id)
        try:
            payload = self._deobfuscate(blob_match.group(1))
        except ValueError as exc:
            raise ProviderChanged(str(exc)) from exc

        master_url = self._pick_source_url(payload)
        if not master_url:
            raise StreamNotFound("HiAnime payload contained no HLS source.")
        subtitle_tracks = self._subtitle_tracks(payload)
        default_subtitle = self._default_subtitle(subtitle_tracks)
        subtitle = default_subtitle.url if default_subtitle else None
        subtitle_language = default_subtitle.language if default_subtitle else None
        subtitle_label = default_subtitle.label if default_subtitle else None
        try:
            master = self.http.get(master_url, referer=referer)
        except HttpError as exc:
            raise ProviderUnavailable(f"HiAnime HLS host failed: {exc}") from exc
        streams = self._parse_master(master, master_url)
        if not streams:
            streams = [Stream(quality="auto", url=master_url)]
        return StreamBundle(
            streams=streams,
            subtitle=subtitle,
            referer=referer,
            mal_id=mal_id,
            provider=self.name,
            subtitle_language=subtitle_language,
            subtitle_label=subtitle_label,
            subtitles=subtitle_tracks,
        )



# ---------- AniLight experimental provider ----------

class AniLightProvider(Provider):
    """AniLight catalog + currently playable direct-source adapter.

    AniLight exposes catalog/episode JSON through api.anilight.live. Its source
    endpoint fans out to several upstream servers. For the initial ani-py
    adapter we deliberately use the "ryu" / AnimeGG route because it returns
    progressive MP4 files and AniLight's own proxy makes those URLs portable
    across desktop and Android players without provider-specific HLS surgery.

    Other AniLight backends expose soft subtitles and broader coverage, but
    currently require changing CDN/proxy rules. Keep those out until their
    playback behavior is proven against ani-py's desktop and Android paths.
    """

    name = "anilight"
    display_name = "AniLight"
    capabilities = ProviderCapabilities(
        sub=True, dub=True, subtitles=False, qualities=True, mal_id=True
    )
    experimental = True

    SOURCE_PROVIDER = "ryu"

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.base = os.getenv("ANI_PY_ANILIGHT_URL", ANILIGHT_BASE_URL).rstrip("/")
        self.api = os.getenv("ANI_PY_ANILIGHT_API_URL", ANILIGHT_API_URL).rstrip("/")
        self._available: Optional[bool] = None
        self._watch_cache: dict[str, dict[str, object]] = {}
        self._episode_cache: dict[str, dict[str, dict[str, object]]] = {}
        self._info_cache: dict[str, dict[str, object]] = {}

    @property
    def api_headers(self) -> dict[str, str]:
        return {
            "Origin": self.base,
            "Accept": "application/json,text/plain,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _api_json(self, path: str, *, timeout: int = 15) -> object:
        try:
            return self.http.get_json(
                self.api + path,
                headers=self.api_headers,
                referer=self.base + "/",
                timeout=timeout,
            )
        except HttpError as exc:
            raise ProviderUnavailable(f"AniLight API request failed: {exc}") from exc

    @staticmethod
    def _title(item: dict[str, object]) -> str:
        title = item.get("title")
        if isinstance(title, dict):
            for key in ("english", "romaji", "native"):
                value = title.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(title, str) and title.strip():
            return title.strip()
        for key in ("name", "englishName"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _parse_provider_id(provider_id: str) -> tuple[Optional[str], str]:
        raw = str(provider_id or "")
        if ":" in raw:
            anilist_id, slug = raw.split(":", 1)
            if anilist_id.isdigit() and slug:
                return anilist_id, slug
        return None, raw

    @staticmethod
    def _provider_id_for(item: dict[str, object]) -> Optional[str]:
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            return None
        ident = item.get("anilistId") or item.get("anilist_id")
        return f"{ident}:{slug}" if ident is not None else slug

    @staticmethod
    def _results(data: object) -> list[dict[str, object]]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("results", "data", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        return []

    def available(self) -> bool:
        """Memoized API preflight for user-configured automatic failover."""
        if self._available is not None:
            return self._available
        try:
            data = self._api_json("/search?" + urlencode({"q": "naruto"}), timeout=12)
            self._available = bool(self._results(data))
            if not self._available:
                warn("AniLight preflight returned no search results; treating it as unavailable.")
        except ProviderError as exc:
            warn(f"AniLight preflight failed ({exc}); treating it as unavailable.")
            self._available = False
        return self._available

    def search(self, query: str) -> list[Anime]:
        data = self._api_json("/search?" + urlencode({"q": query}))
        rows = self._results(data)
        found: list[Anime] = []
        seen: set[str] = set()
        for item in rows:
            provider_id = self._provider_id_for(item)
            title = self._title(item)
            if not provider_id or not title or provider_id in seen:
                continue
            self._info_cache[provider_id] = item
            found.append(Anime(provider_id=provider_id, title=title, provider=self.name))
            seen.add(provider_id)
        return found

    @staticmethod
    def _number(value: object) -> Optional[str]:
        return _episode_number(value)

    def _watch_doc(self, anime: Anime | str) -> dict[str, object]:
        provider_id = _provider_id(anime)
        cached = self._watch_cache.get(provider_id)
        if cached is not None:
            return cached
        _, slug = self._parse_provider_id(provider_id)
        if not slug:
            raise AnimeNotFound("AniLight anime id did not contain a slug.")
        data = self._api_json("/watch/" + quote(slug, safe="-._~"), timeout=20)
        if not isinstance(data, dict):
            raise ProviderChanged("AniLight watch response was not an object.")
        self._watch_cache[provider_id] = data
        return data

    def _episode_rows(self, anime: Anime | str) -> dict[str, dict[str, object]]:
        provider_id = _provider_id(anime)
        cached = self._episode_cache.get(provider_id)
        if cached is not None:
            return cached

        data = self._watch_doc(anime)
        raw = data.get("episodes")
        if not isinstance(raw, list):
            nested = data.get("data")
            raw = nested.get("episodes") if isinstance(nested, dict) else None
        if not isinstance(raw, list):
            raise ProviderChanged("AniLight watch response did not contain an episode list.")

        rows: dict[str, dict[str, object]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            number = self._number(item.get("number"))
            if number:
                rows[number] = item
        self._episode_cache[provider_id] = rows
        return rows

    def episodes(self, anime: Anime | str) -> list[Episode]:
        rows = self._episode_rows(anime)
        episodes = [Episode(episode_id=number, number=number) for number in rows]
        episodes.sort(
            key=lambda e: float(e.number)
            if re.fullmatch(r"\d+(?:\.\d+)?", e.number)
            else 10**9
        )
        if not episodes:
            raise EpisodeNotFound(f"AniLight returned no episodes for {_provider_id(anime)}.")
        return episodes

    def _numeric_id(self, anime: Anime | str) -> Optional[str]:
        provider_id = _provider_id(anime)
        watch = self._watch_doc(anime)
        ident = watch.get("id")
        if ident is not None:
            return str(ident)

        cached = self._info_cache.get(provider_id)
        if not isinstance(cached, dict):
            _, slug = self._parse_provider_id(provider_id)
            if not slug:
                return None
            data = self._api_json("/anime/" + quote(slug, safe="-._~"), timeout=12)
            cached = data if isinstance(data, dict) else None
            if isinstance(cached, dict):
                self._info_cache[provider_id] = cached
        if not isinstance(cached, dict):
            return None
        ident = cached.get("id")
        return str(ident) if ident is not None else None

    def _mal_id(self, anime: Anime | str) -> Optional[str]:
        provider_id = _provider_id(anime)
        cached = self._info_cache.get(provider_id)
        if not isinstance(cached, dict):
            _, slug = self._parse_provider_id(provider_id)
            if not slug:
                return None
            try:
                data = self._api_json("/anime/" + quote(slug, safe="-._~"), timeout=12)
            except ProviderError:
                return None
            cached = data if isinstance(data, dict) else None
            if isinstance(cached, dict):
                self._info_cache[provider_id] = cached
        if not isinstance(cached, dict):
            return None
        value = cached.get("idMal") or cached.get("malId") or cached.get("mal_id")
        return str(value) if value is not None else None

    def _sources(self, anime_id: str, episode: str, mode: str) -> dict[str, object]:
        path = "/sources?" + urlencode({
            "id": anime_id,
            "epNum": episode,
            "type": mode,
            "providerId": self.SOURCE_PROVIDER,
        })
        data = self._api_json(path, timeout=20)
        if not isinstance(data, dict):
            raise ProviderChanged("AniLight source response was not an object.")
        return data

    @staticmethod
    def _quality(value: object) -> str:
        text = str(value or "auto").strip()
        if re.fullmatch(r"\d{3,4}", text):
            return text + "p"
        return text or "auto"

    def _proxy_url(self, upstream: str) -> str:
        # AniLight's stable API endpoint chooses the current worker/CDN for the
        # AnimeGG route. Do not hardcode the rotating worker hostname itself.
        return self.api + "/proxy/ryu?" + urlencode({"url": upstream})

    def resolve(self, anime: Anime | str, episode: Episode, mode: str) -> StreamBundle:
        if mode not in {"sub", "dub"}:
            raise StreamNotFound(f"AniLight does not support mode {mode!r}.")

        row = self._episode_rows(anime).get(episode.number)
        if not row:
            raise EpisodeNotFound(f"AniLight episode {episode.number} was not found.")

        embeds = row.get("embed_url")
        if isinstance(embeds, dict):
            marker = embeds.get(mode)
            if not isinstance(marker, str) or not marker:
                raise StreamNotFound(f"AniLight has no {mode} version for episode {episode.number}.")

        anime_id = self._numeric_id(anime)
        if not anime_id:
            raise ProviderChanged("AniLight watch response did not expose its numeric anime id.")

        payload = self._sources(anime_id, episode.number, mode)
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            raise ProviderChanged("AniLight source response did not contain a sources list.")

        streams: list[Stream] = []
        seen: set[str] = set()
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            upstream = item.get("url") or item.get("file")
            if not isinstance(upstream, str) or not upstream.startswith(("http://", "https://")):
                continue
            proxied = self._proxy_url(upstream)
            if proxied in seen:
                continue
            streams.append(Stream(self._quality(item.get("quality")), proxied))
            seen.add(proxied)

        if not streams:
            raise StreamNotFound(
                f"AniLight's portable source has no {mode} stream for episode {episode.number}."
            )

        streams.sort(key=stream_rank, reverse=True)
        return StreamBundle(
            streams=streams,
            subtitle=None,
            referer=self.base + "/",
            mal_id=self._mal_id(anime),
            provider=self.name,
        )


# ---------- AnimeAV1 experimental provider ----------

class AnimeAV1Provider(Provider):
    """AnimeAV1: Spanish catalog whose video carries burned-in Latin subtitles.

    The site is SvelteKit, so every page has a JSON twin at ``__data.json``.
    Of its mirrors only MP4Upload hands out a plain file that every player
    and downloader accepts; the others need per-host extraction and are left
    out until one is proven portable.
    """

    name = "animeav1"
    display_name = "AnimeAV1"
    capabilities = ProviderCapabilities(
        sub=True, dub=True, subtitles=False, qualities=False, mal_id=True
    )
    experimental = True

    VARIANTS = {"sub": "SUB", "dub": "DUB"}

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.base = os.getenv("ANI_PY_ANIMEAV1_URL", ANIMEAV1_BASE_URL).rstrip("/")
        self._media_cache: dict[str, dict[str, object]] = {}

    def _page(self, path: str) -> dict[str, object]:
        try:
            payload = self.http.get_json(self.base + path, referer=self.base + "/", timeout=20)
        except HttpError as exc:
            raise ProviderUnavailable(f"AnimeAV1 request failed: {exc}") from exc
        return _sveltekit_data(payload)

    def search(self, query: str) -> list[Anime]:
        data = self._page("/catalogo/__data.json?" + urlencode({"search": query}))
        results = data.get("results")
        found: list[Anime] = []
        seen: set[str] = set()
        for item in results if isinstance(results, list) else []:
            if not isinstance(item, dict):
                continue
            slug, title = item.get("slug"), item.get("title")
            if not isinstance(slug, str) or not slug or not isinstance(title, str) or not title.strip():
                continue
            if slug not in seen:
                found.append(Anime(provider_id=slug, title=title.strip(), provider=self.name))
                seen.add(slug)
        return found

    def _media(self, anime: Anime | str) -> dict[str, object]:
        slug = _provider_id(anime)
        cached = self._media_cache.get(slug)
        if cached is not None:
            return cached
        media = self._page(f"/media/{quote(slug, safe='-._~')}/__data.json").get("media")
        if not isinstance(media, dict):
            raise ProviderChanged("AnimeAV1 media response did not contain a media object.")
        self._media_cache[slug] = media
        return media

    def episodes(self, anime: Anime | str) -> list[Episode]:
        rows = self._media(anime).get("episodes")
        numbers: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            number = _episode_number(row.get("number")) if isinstance(row, dict) else None
            if number:
                numbers.add(number)
        if not numbers:
            raise EpisodeNotFound(f"AnimeAV1 returned no episodes for {_provider_id(anime)}.")
        return [Episode(episode_id=number, number=number) for number in sorted(numbers, key=float)]

    def resolve(self, anime: Anime | str, episode: Episode, mode: str) -> StreamBundle:
        variant = self.VARIANTS.get(mode)
        if variant is None:
            raise StreamNotFound(f"AnimeAV1 does not support mode {mode!r}.")
        slug = quote(_provider_id(anime), safe="-._~")
        data = self._page(f"/media/{slug}/{quote(episode.number, safe='.')}/__data.json")
        embeds = data.get("embeds")
        servers = embeds.get(variant) if isinstance(embeds, dict) else None
        if not isinstance(servers, list) or not servers:
            raise StreamNotFound(f"AnimeAV1 has no {mode} version for episode {episode.number}.")
        embed_url = next(
            (
                row["url"] for row in servers
                if isinstance(row, dict)
                and str(row.get("server") or "").lower() == "mp4upload"
                and isinstance(row.get("url"), str)
            ),
            None,
        )
        if not embed_url:
            raise StreamNotFound(
                f"AnimeAV1 episode {episode.number} has no MP4Upload mirror, the only one ani-py can play."
            )
        url = _resolve_mp4upload(self.http, embed_url, self.base + "/")
        media = data.get("media")
        mal = media.get("malId") if isinstance(media, dict) else None
        mal_id = str(mal) if isinstance(mal, (int, str)) and not isinstance(mal, bool) and str(mal).isdigit() else None
        return StreamBundle(
            streams=[Stream("auto", url)],
            subtitle=None,
            referer=MP4UPLOAD_REFERER,
            mal_id=mal_id,
            provider=self.name,
        )


# ---------- AnimeFLV (animeflv.or.at) experimental provider ----------

class AnimeFlvProvider(Provider):
    """animeflv.or.at: a small WordPress catalog of recent Latin-subbed episodes.

    Each anime is a WordPress category and each episode a post, so search and
    episode lists come from the site's public REST API. The mirrors sit in the
    episode page as base64 ``data-src`` buttons; as with AnimeAV1, which this
    site imports from, only MP4Upload gives a file every player accepts.
    """

    name = "animeflv"
    display_name = "AnimeFLV"
    capabilities = ProviderCapabilities(
        sub=True, dub=False, subtitles=False, qualities=False, mal_id=False
    )
    experimental = True

    PAGE_SIZE = 100
    MAX_PAGES = 30
    _BUTTON_RE = re.compile(r'tooltip-text">([^<]*)</span>\s*(<button[^>]*>)', re.IGNORECASE)
    _EPISODE_SLUG_RE = re.compile(r"-episodio-(\d+(?:-\d+)?)$")

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.base = os.getenv("ANI_PY_ANIMEFLV_URL", ANIMEFLV_BASE_URL).rstrip("/")

    def _api(self, path: str) -> object:
        try:
            return self.http.get_json(
                f"{self.base}/wp-json/wp/v2{path}", referer=self.base + "/", timeout=20
            )
        except HttpError as exc:
            raise ProviderUnavailable(f"AnimeFLV request failed: {exc}") from exc

    def search(self, query: str) -> list[Anime]:
        rows = self._api("/categories?" + urlencode(
            {"search": query, "per_page": 20, "_fields": "id,name,slug,count"}
        ))
        found: list[Anime] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            ident, slug, name = row.get("id"), row.get("slug"), row.get("name")
            if not isinstance(ident, int) or isinstance(ident, bool):
                continue
            if not isinstance(slug, str) or not isinstance(name, str) or not name.strip():
                continue
            # "anime" is the catch-all category every episode also belongs to.
            if slug == "anime" or not row.get("count"):
                continue
            found.append(Anime(
                provider_id=f"{ident}:{slug}", title=html.unescape(name).strip(), provider=self.name,
            ))
        return found

    def episodes(self, anime: Anime | str) -> list[Episode]:
        category = _provider_id(anime).split(":", 1)[0]
        if not category.isdigit():
            raise AnimeNotFound("AnimeFLV anime id did not contain a category id.")
        links: dict[str, str] = {}
        for page in range(1, self.MAX_PAGES + 1):
            query = urlencode(
                {"categories": category, "per_page": self.PAGE_SIZE, "page": page, "_fields": "slug,link"}
            )
            try:
                rows = self._api("/posts?" + query)
            except ProviderUnavailable:
                if page == 1:
                    raise
                # WordPress answers 400 for a page past the last one.
                break
            if not isinstance(rows, list):
                raise ProviderChanged("AnimeFLV post list was not a list.")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                slug, link = row.get("slug"), row.get("link")
                match = self._EPISODE_SLUG_RE.search(slug) if isinstance(slug, str) else None
                if not match or not isinstance(link, str) or not link.startswith(self.base + "/"):
                    continue
                links.setdefault(match.group(1).replace("-", "."), link)
            if len(rows) < self.PAGE_SIZE:
                break
        if not links:
            raise EpisodeNotFound(f"AnimeFLV returned no episodes for {_provider_id(anime)}.")
        return [
            Episode(episode_id=links[number], number=number)
            for number in sorted(links, key=float)
        ]

    def resolve(self, anime: Anime | str, episode: Episode, mode: str) -> StreamBundle:
        if mode != "sub":
            raise StreamNotFound("AnimeFLV only offers subtitled video.")
        link = episode.episode_id
        if not link.startswith(self.base + "/"):
            raise EpisodeNotFound(f"AnimeFLV episode {episode.number} has no page on this site.")
        try:
            page = self.http.get(link, referer=self.base + "/")
        except HttpError as exc:
            raise ProviderUnavailable(f"AnimeFLV episode page failed: {exc}") from exc
        embed_url: Optional[str] = None
        for server_name, tag in self._BUTTON_RE.findall(page):
            if server_name.strip().lower() != "mp4upload":
                continue
            encoded = _attrs(tag).get("data-src", "")
            try:
                embed_url = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True).decode("utf-8")
            except ValueError:
                continue
            break
        if not embed_url:
            raise StreamNotFound(
                f"AnimeFLV episode {episode.number} has no MP4Upload mirror, the only one ani-py can play."
            )
        url = _resolve_mp4upload(self.http, embed_url, self.base + "/")
        return StreamBundle(
            streams=[Stream("auto", url)],
            subtitle=None,
            referer=MP4UPLOAD_REFERER,
            mal_id=None,
            provider=self.name,
        )


# ---------- provider manager ----------

class ProviderManager:
    def __init__(self, providers: Sequence[Provider], order: Sequence[str]) -> None:
        self.providers = {p.name: p for p in providers}
        cleaned = [name for name in order if name in self.providers]
        self.order = cleaned or list(self.providers)

    def get(self, name: str) -> Provider:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise ProviderUnavailable(f"Unknown provider: {name}") from exc

    def search(self, query: str, preference: str = "auto") -> list[Anime]:
        names = self.order if preference == "auto" else [preference]
        errors: list[str] = []
        for index, name in enumerate(names):
            provider = self.get(name)
            if preference == "auto" and not provider.available():
                warn(f"Skipping {provider.display_name}: preflight unavailable.")
                continue
            try:
                results = provider.search(query)
            except ProviderError as exc:
                errors.append(f"{provider.display_name}: {exc}")
                if preference == "auto" and index + 1 < len(names):
                    warn(f"{provider.display_name} search failed; trying {self.get(names[index + 1]).display_name}.")
                continue
            if results:
                return results
            if preference == "auto" and index + 1 < len(names):
                warn(f"No results from {provider.display_name}; trying {self.get(names[index + 1]).display_name}.")
        if errors:
            raise ProviderUnavailable("; ".join(errors))
        return []

    def episodes(self, anime: Anime) -> list[Episode]:
        return self.get(anime.provider).episodes(anime)

    def resolve(self, anime: Anime, episode: Episode, mode: str) -> StreamBundle:
        return self.get(anime.provider).resolve(anime, episode, mode)

    def fallback_names(self, current: str) -> list[str]:
        return [name for name in self.order if name != current and self.get(name).available()]



# ---------- history ----------

class HistoryStore:
    def __init__(self) -> None:
        root = Path(os.getenv("ANI_PY_HIST_DIR") or os.getenv("XDG_STATE_HOME") or (Path.home() / ".local/state"))
        self.dir = root / APP_NAME
        self.path = self.dir / "history.tsv"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        # update() is a read-modify-write, and the auto-next completion watcher
        # writes from a background thread while the main thread may be exiting.
        self._lock = threading.Lock()

    def load(self) -> list[HistoryEntry]:
        out: list[HistoryEntry] = []
        for raw in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split("\t")
            if len(parts) >= 4:
                episode, provider, provider_id = parts[:3]
                title = "\t".join(parts[3:])
                completed = True
                # Titles may legitimately contain tabs, so the state token is
                # recognised by shape at the end rather than by field position.
                head, sep, tail = title.rpartition("\t")
                if sep and head and tail in ("state=0", "state=1"):
                    title = head
                    completed = tail == "state=1"
                out.append(HistoryEntry(episode, provider, provider_id, title, completed))
            elif len(parts) == 3:
                # v0.3 and older: episode, HiAnime slug, title
                episode, provider_id, title = parts
                out.append(HistoryEntry(episode, "hianime", provider_id, title))
        return out

    def save(self, entries: Sequence[HistoryEntry]) -> None:
        data = "".join(
            f"{e.episode}\t{e.provider}\t{e.provider_id}\t{e.title}"
            f"\tstate={1 if e.completed else 0}\n"
            for e in entries
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.dir, delete=False) as tf:
            tf.write(data)
            temp_name = tf.name
        Path(temp_name).replace(self.path)

    def update(self, anime: Anime, episode: str, completed: bool = False) -> None:
        with self._lock:
            entries = self.load()
            for index, item in enumerate(entries):
                if item.provider == anime.provider and item.provider_id == anime.provider_id:
                    item.episode = episode
                    item.title = anime.title
                    item.completed = completed
                    # Most recent first: --continue and the machine-readable
                    # history both read "the last thing watched" from row 0.
                    entries.insert(0, entries.pop(index))
                    break
            else:
                entries.insert(
                    0, HistoryEntry(episode, anime.provider, anime.provider_id, anime.title, completed)
                )
            self.save(entries)

    def clear(self) -> None:
        self.path.write_text("", encoding="utf-8")


def _history_json(entry: HistoryEntry) -> dict[str, object]:
    return {
        "provider": entry.provider,
        "id": entry.provider_id,
        "title": entry.title,
        "episode": entry.episode,
        "completed": entry.completed,
    }


def headless_conflict(args: argparse.Namespace) -> Optional[str]:
    """Why --headless cannot run with these flags, or None."""
    for attr, flag in (
        ("attach", "--attach"),
        ("no_detach", "--no-detach"),
        ("exit_after_play", "--exit-after-play"),
        ("continue_watching", "--continue"),
    ):
        if getattr(args, attr, False):
            return f"--headless cannot be combined with {flag}."
    if not getattr(args, "episode", None):
        return "--headless needs -e/--episode."
    return None


def _subtitle_json(track: Optional[SubtitleTrack]) -> Optional[dict[str, object]]:
    if track is None:
        return None
    return {"language": track.language, "label": track.label, "source": track.source}


class DetachedSessionStore:
    def __init__(self) -> None:
        root = Path(os.getenv("ANI_PY_HIST_DIR") or os.getenv("XDG_STATE_HOME") or (Path.home() / ".local/state"))
        self.dir = root / APP_NAME
        self.path = self.dir / "detached-session.json"
        self.dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> Optional[DetachedSession]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        required = {
            "socket", "player", "provider", "provider_id", "title",
            "episode", "quality", "mode", "source_provider",
        }
        if not required.issubset(raw):
            return None
        try:
            return DetachedSession(
                socket=str(raw["socket"]),
                player=str(raw["player"]),
                provider=str(raw["provider"]),
                provider_id=str(raw["provider_id"]),
                title=str(raw["title"]),
                episode=str(raw["episode"]),
                quality=str(raw["quality"]),
                mode=str(raw["mode"]),
                source_provider=str(raw["source_provider"]),
                subtitle_preference=str(raw.get("subtitle_preference") or "auto"),
            )
        except (TypeError, ValueError):
            return None

    def save(self, session: DetachedSession) -> None:
        payload = json.dumps(dataclasses.asdict(session), ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.dir, delete=False
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        temp_path = Path(temp_name)
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        temp_path.replace(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# Window restore follows mpv's geometry string format: "<width>x<height>"
# (both may be "<n>%" relative to the screen), optionally followed by a
# "+X+Y" or "-X-Y" placement. Anything not matching this is ignored rather
# than re-applied, so a corrupted state file can never generate a bad flag.
_WINDOW_GEOMETRY_RE = re.compile(r"^[0-9.%]+x[0-9.%]+([+-][0-9.%]+)?([+-][0-9.%]+)?$")
_WINDOW_AUTOFIT_RE = re.compile(r"^[0-9.%]+x[0-9.%]+$")


class WindowStateStore:
    """Persist the last mpv window shape across ani-py runs.

    mpv itself only applies window options from the command line while it
    starts; a restart is triggered by the auto-next fallback, a dropped IPC
    session, or a fresh ``ani-py`` invocation. Without persisted state each
    of those re-opened mpv at the configured default size (``video=no`` plus
    the user's ``geometry=``), losing fullscreen and the last window size.
    """

    def __init__(self) -> None:
        root = Path(os.getenv("ANI_PY_HIST_DIR") or os.getenv("XDG_STATE_HOME") or (Path.home() / ".local/state"))
        self.dir = root / APP_NAME
        self.path = self.dir / "window-state.json"
        self.dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, object]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, object] = {}
        if raw.get("fullscreen") is True or raw.get("fullscreen") is False:
            out["fullscreen"] = raw["fullscreen"]
        geometry = raw.get("geometry")
        if isinstance(geometry, str) and geometry and _WINDOW_GEOMETRY_RE.match(geometry) is not None:
            out["geometry"] = geometry
        autofit = raw.get("autofit")
        if isinstance(autofit, str) and autofit and _WINDOW_AUTOFIT_RE.match(autofit) is not None:
            out["autofit"] = autofit
        return out

    def save(self, data: dict[str, object]) -> None:
        payload = json.dumps({k: v for k, v in data.items()}, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.dir, delete=False
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        Path(temp_name).replace(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# ---------- menu ----------

class Menu:
    def __init__(self, program: Optional[str], extra_flags: str = "") -> None:
        requested = program or os.getenv("ANI_PY_MENU") or "fzf"
        self.program = requested if shutil.which(requested) else None
        self.extra = split_flags(extra_flags or os.getenv("ANI_PY_MENU_FLAGS", ""))

    def choose(
        self,
        items: Sequence[str],
        prompt: str,
        *,
        multi: bool = False,
        compact: bool = False,
        header: Optional[str] = None,
    ) -> list[str]:
        if not items:
            return []
        if len(items) == 1 and not compact:
            return [items[0]]
        if self.program == "fzf":
            height = "~12" if compact else "80%"
            info = "hidden" if compact else "inline-right"
            cmd = [
                "fzf", "--ansi", "--reverse", "--cycle",
                f"--height={height}", "--border=rounded", f"--info={info}",
                "--pointer=›", "--prompt", prompt,
            ]
            if header:
                cmd += ["--header", header, "--header-first"]
            if multi:
                cmd += ["--multi", "--bind", "tab:toggle+down", "--marker=✓"]
            cmd += self.extra
            proc = run_capture(cmd, input_text="\n".join(items) + "\n")
            return proc.stdout.splitlines() if proc.returncode == 0 else []
        if self.program == "rofi":
            cmd = ["rofi", "-dmenu", "-i", "-p", prompt.rstrip()] + self.extra
            if multi:
                cmd += ["-multi-select"]
            proc = run_capture(cmd, input_text="\n".join(items) + "\n")
            return proc.stdout.splitlines() if proc.returncode == 0 else []
        if self.program == "dmenu":
            lines = str(min(len(items), 8 if compact else 20))
            cmd = ["dmenu", "-l", lines, "-p", prompt.rstrip()] + self.extra
            proc = run_capture(cmd, input_text="\n".join(items) + "\n")
            return proc.stdout.splitlines() if proc.returncode == 0 else []
        if header:
            print(f"\n{header}")
        return self._numbered(items, prompt, multi=multi)

    def _numbered(self, items: Sequence[str], prompt: str, *, multi: bool) -> list[str]:
        print()
        for i, item in enumerate(items, 1):
            print(f"  {sty(str(i).rjust(3), C.CYAN)}  {item}")
        print()
        suffix = " (comma/range, q to cancel)" if multi else " (q to cancel)"
        raw = input(sty(prompt + suffix + " ", C.BOLD)).strip()
        if not raw or raw.lower() in {"q", "quit", "exit"}:
            return []
        if not multi:
            try:
                idx = int(raw)
                return [items[idx - 1]] if 1 <= idx <= len(items) else []
            except ValueError:
                # Allow exact text entry.
                return [raw] if raw in items else []

        picks: list[int] = []
        for token in re.split(r"[ ,]+", raw):
            if not token:
                continue
            if "-" in token:
                try:
                    a, b = (int(x) for x in token.split("-", 1))
                except ValueError:
                    continue
                step = 1 if a <= b else -1
                picks.extend(range(a, b + step, step))
            else:
                try:
                    picks.append(int(token))
                except ValueError:
                    continue
        return [items[i - 1] for i in picks if 1 <= i <= len(items)]


# ---------- Android / Termux intent relay ----------

def is_android_environment() -> bool:
    return bool(os.getenv("ANDROID_ROOT") or os.getenv("TERMUX_VERSION"))


def _android_user_id() -> str:
    raw = os.getenv("TERMUX__USER_ID", "0")
    return raw if raw.isdigit() else "0"


def _relay_encode(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _relay_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


_SUBTITLE_SUFFIXES = (".vtt", ".srt", ".ass", ".ssa")
_SUBTITLE_MIME = {
    ".vtt": "text/vtt; charset=utf-8",
    ".srt": "application/x-subrip; charset=utf-8",
    ".ass": "text/x-ssa; charset=utf-8",
    ".ssa": "text/x-ssa; charset=utf-8",
}


def _subtitle_suffix_for(url: str) -> str:
    """Cosmetic relay suffix for a subtitle URL; defaults to .vtt."""
    path = urlsplit(url).path.lower()
    for suffix in _SUBTITLE_SUFFIXES:
        if path.endswith(suffix):
            return suffix
    return ".vtt"


def _is_remote_subtitle(subtitle: str) -> bool:
    """False for a subtitle an external source already saved to disk."""
    return urlsplit(subtitle).scheme in {"http", "https"}


def _subtitle_mime_for_suffix(suffix: str) -> str:
    return _SUBTITLE_MIME.get(suffix.lower(), "text/vtt; charset=utf-8")


def _hls_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


_ANDROID_SUBTITLE_GROUP = "ani-py-subs"


def _subtitle_media_tag(
    subtitle_url: str,
    *,
    language: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    name = label or ("English" if language == "en" else "External")
    attrs = [
        "TYPE=SUBTITLES",
        f'GROUP-ID="{_ANDROID_SUBTITLE_GROUP}"',
        f'NAME="{_hls_attr(name)}"',
    ]
    if language:
        attrs.append(f'LANGUAGE="{_hls_attr(language)}"')
    attrs += [
        "DEFAULT=YES",
        "AUTOSELECT=YES",
        f'URI="{_hls_attr(subtitle_url)}"',
    ]
    return "#EXT-X-MEDIA:" + ",".join(attrs)


def _rewrite_hls_manifest(
    manifest: str,
    base_url: str,
    localize,
    subtitle_url: Optional[str] = None,
    subtitle_language: Optional[str] = None,
    subtitle_label: Optional[str] = None,
) -> str:
    """Route remote HLS URIs through localhost and safely attach one subtitle rendition."""
    uri_attr = re.compile(r'URI="([^"]+)"')
    out: list[str] = []
    has_stream_inf = False
    upstream_has_subtitles = bool(
        re.search(r"#EXT-X-MEDIA:[^\n\r]*TYPE=SUBTITLES", manifest, re.IGNORECASE)
        or re.search(r"SUBTITLES\s*=", manifest, re.IGNORECASE)
    )
    inject_subtitle = bool(subtitle_url) and not upstream_has_subtitles

    def proxied(value: str) -> str:
        absolute = urljoin(base_url, value)
        return localize(absolute) if urlsplit(absolute).scheme in {"http", "https"} else value

    for raw in manifest.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("#EXT-X-STREAM-INF"):
            has_stream_inf = True
            if inject_subtitle and "SUBTITLES=" not in line:
                line = f'{line},SUBTITLES="{_ANDROID_SUBTITLE_GROUP}"'
        if line.startswith("#"):
            line = uri_attr.sub(lambda m: f'URI="{proxied(m.group(1))}"', line)
        elif line.strip():
            line = proxied(line.strip())
        out.append(line)

    if inject_subtitle and has_stream_inf and subtitle_url:
        media = _subtitle_media_tag(
            subtitle_url,
            language=subtitle_language,
            label=subtitle_label,
        )
        if out and out[0] == "#EXTM3U":
            out.insert(1, media)
        else:
            out.insert(0, media)
    return "\n".join(out) + ("\n" if manifest.endswith(("\n", "\r")) else "")


def _wrap_hls_media_playlist(
    variant_url: str,
    subtitle_playlist_url: str,
    *,
    subtitle_language: Optional[str] = None,
    subtitle_label: Optional[str] = None,
) -> str:
    """Wrap a variant media playlist URL in a single-variant master playlist."""
    media = _subtitle_media_tag(
        subtitle_playlist_url,
        language=subtitle_language,
        label=subtitle_label,
    )
    # BANDWIDTH is required by EXT-X-STREAM-INF; this is a single-variant
    # wrapper, so the value is only a protocol placeholder estimate.
    return (
        "#EXTM3U\n"
        f"{media}\n"
        f'#EXT-X-STREAM-INF:BANDWIDTH=2000000,SUBTITLES="{_ANDROID_SUBTITLE_GROUP}"\n'
        f"{variant_url}\n"
    )


_EXTINF_RE = re.compile(r"#EXTINF:([0-9]+(?:\.[0-9]+)?)")


def _hls_media_duration(text: str) -> Optional[int]:
    """Sum EXTINF durations, rounded up; None when no usable entries exist."""
    total = sum(value for value in (float(item) for item in _EXTINF_RE.findall(text)) if value > 0)
    if total <= 0:
        return None
    return int(total) if float(total).is_integer() else int(total) + 1


def _subtitle_playlist(segment_url: str, duration: int) -> str:
    """Build a VOD WebVTT rendition playlist around one complete segment."""
    total = max(1, duration)
    return (
        "#EXTM3U\n"
        f"#EXT-X-TARGETDURATION:{total}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        f"#EXTINF:{total},\n"
        f"{segment_url}\n"
        "#EXT-X-ENDLIST\n"
    )


class _AndroidRelayHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address, handler, *, token: str, referer: str, user_agent: str,
        debug: bool = False, log_path: Optional[str] = None,
        subtitle_target: Optional[str] = None,
        subtitle_language: Optional[str] = None,
        subtitle_label: Optional[str] = None,
        allowed_targets: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(address, handler)
        self.token = token
        self.referer = referer
        self.user_agent = user_agent
        self.debug = debug
        self.log_path = log_path
        self.subtitle_target = subtitle_target
        self.subtitle_language = subtitle_language
        self.subtitle_label = subtitle_label
        self.allowed_targets = set(allowed_targets) if allowed_targets is not None else None
        self.last_activity = time.monotonic()

    def _allow_target(self, target: str) -> None:
        if self.allowed_targets is not None:
            self.allowed_targets.add(target)

    def target_allowed(self, target: str) -> bool:
        return self.allowed_targets is None or target in self.allowed_targets

    def relay_url(self, target: str) -> str:
        self._allow_target(target)
        host, port = self.server_address[:2]
        return f"http://127.0.0.1:{port}/{self.token}/{_relay_encode(target)}"

    def subtitle_relay_url(self, target: str) -> str:
        self._allow_target(target)
        host, port = self.server_address[:2]
        suffix = _subtitle_suffix_for(target)
        return f"http://127.0.0.1:{port}/{self.token}/subtitle/{_relay_encode(target)}{suffix}"

    def subtitle_playlist_url_for(self, target: str, duration: int) -> str:
        host, port = self.server_address[:2]
        suffix = _subtitle_suffix_for(target)
        total = max(1, int(duration))
        return f"http://127.0.0.1:{port}/{self.token}/sublist/{total}/{_relay_encode(target)}{suffix}"

    def variant_url_for(self, target: str) -> str:
        # Same relay route with a flag so the handler serves the raw variant
        # media playlist instead of wrapping it in a master again.
        return self.relay_url(target) + "?variant=1"

    def _debug_log(self, message: str) -> None:
        if not self.debug or not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(message.rstrip() + "\n")
                handle.flush()
        except OSError:
            pass


class _AndroidRelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - must match BaseHTTPRequestHandler signature
        return

    @property
    def relay(self) -> _AndroidRelayHTTPServer:
        return self.server  # type: ignore[return-value]

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def _parse_relay_path(self) -> Optional[tuple[str, str, Optional[str], Optional[int]]]:
        """Return (upstream_target, kind, suffix, duration) or None when invalid."""
        prefix = f"/{self.relay.token}/"
        path = self.path.split("?", 1)[0]
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        kind = "video"
        duration: Optional[int] = None
        suffix: Optional[str] = None
        if rest.startswith("subtitle/"):
            kind = "subtitle"
            rest = rest[len("subtitle/"):]
        elif rest.startswith("sublist/"):
            kind = "sublist"
            rest = rest[len("sublist/"):]
            duration_text, sep, rest = rest.partition("/")
            if not sep or not duration_text.isdigit():
                return None
            duration = int(duration_text)
        if kind in ("subtitle", "sublist"):
            for candidate in _SUBTITLE_SUFFIXES:
                if rest.lower().endswith(candidate):
                    suffix = candidate
                    rest = rest[: -len(candidate)]
                    break
            if suffix is None:
                return None
        try:
            target = _relay_decode(rest)
        except Exception:
            return None
        if urlsplit(target).scheme not in {"http", "https"}:
            return None
        if not self.relay.target_allowed(target):
            return None
        return target, kind, suffix, duration

    def _target(self) -> Optional[str]:
        parsed = self._parse_relay_path()
        return parsed[0] if parsed else None

    def _serve(self, *, send_body: bool) -> None:
        method = "GET" if send_body else "HEAD"
        parsed = self._parse_relay_path()
        if not parsed:
            self.relay._debug_log(f"[android-relay] video {method} invalid -> 403")
            self.send_error(403)
            return
        target, kind, sub_suffix, sub_duration = parsed
        if kind == "sublist":
            self.relay.last_activity = time.monotonic()
            playlist = _subtitle_playlist(
                self.relay.subtitle_relay_url(target), sub_duration or 0,
            ).encode("utf-8")
            self.relay._debug_log(
                f"[android-relay] subtitle {method} playlist -> 200 application/vnd.apple.mpegurl"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(playlist)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if send_body:
                self.wfile.write(playlist)
            return
        if kind == "subtitle":
            req_desc = f"subtitle {method} {sub_suffix}"
        else:
            req_desc = f"video {method} segment"
        self.relay.last_activity = time.monotonic()
        headers = {
            "User-Agent": self.relay.user_agent,
            "Accept-Encoding": "identity",
        }
        if self.relay.referer:
            headers["Referer"] = self.relay.referer
        likely_hls = ".m3u8" in urlsplit(target).path.lower()
        for name in ("Range", "If-Range", "If-None-Match", "If-Modified-Since"):
            if likely_hls and name in {"Range", "If-Range"}:
                continue
            value = self.headers.get(name)
            if value:
                headers[name] = value

        req = urllib_request.Request(target, headers=headers, method=method)
        try:
            upstream = urllib_request.urlopen(req, timeout=30)
        except urllib_error.HTTPError as exc:
            # Some CDNs reject HEAD; retry a lightweight GET for metadata.
            if not send_body and exc.code in {400, 403, 405, 501}:
                try:
                    upstream = urllib_request.urlopen(
                        urllib_request.Request(target, headers=headers, method="GET"), timeout=30
                    )
                except Exception as inner:
                    self.relay._debug_log(f"[android-relay] {req_desc} -> 502")
                    self.send_error(502, str(inner))
                    return
            else:
                self.relay._debug_log(f"[android-relay] {req_desc} -> {exc.code}")
                self.send_error(exc.code, str(exc.reason))
                return
        except Exception as exc:
            self.relay._debug_log(f"[android-relay] {req_desc} -> 502")
            self.send_error(502, str(exc))
            return

        try:
            status = getattr(upstream, "status", 200) or 200
            final_url = upstream.geturl()
            content_type = upstream.headers.get("Content-Type", "application/octet-stream")
            is_hls = (
                ".m3u8" in urlsplit(final_url).path.lower()
                or "mpegurl" in content_type.lower()
            )

            if is_hls and kind == "video":
                self.relay._debug_log(
                    f"[android-relay] video {method} hls -> {status} "
                    f"{content_type.split(';', 1)[0] or 'application/octet-stream'}"
                )

            if kind == "subtitle":
                # Subtitle bytes pass through unchanged, but the MIME type is
                # forced from the relay suffix: some CDNs serve subtitles as
                # application/octet-stream, which VLC for Android ignores.
                mime = _subtitle_mime_for_suffix(sub_suffix or ".vtt")
                raw = upstream.read()
                self.relay._debug_log(
                    f"[android-relay] subtitle {method} {sub_suffix} -> {status} {mime.split(';', 1)[0]}"
                )
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                if send_body:
                    try:
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return

            if is_hls and send_body:
                raw = upstream.read()
                text = raw.decode("utf-8", "replace")
                hls_subtitle_target = (
                    self.relay.subtitle_target
                    if self.relay.subtitle_target
                    and _subtitle_suffix_for(self.relay.subtitle_target) == ".vtt"
                    else None
                )
                subtitle_url = (
                    self.relay.subtitle_relay_url(hls_subtitle_target)
                    if hls_subtitle_target
                    else None
                )
                variant_request = "variant=1" in urlsplit(self.path).query
                if "#EXT-X-STREAM-INF" in text:
                    body = _rewrite_hls_manifest(
                        text,
                        final_url,
                        self.relay.relay_url,
                        subtitle_url=subtitle_url,
                        subtitle_language=self.relay.subtitle_language,
                        subtitle_label=self.relay.subtitle_label,
                    ).encode("utf-8")
                elif subtitle_url and not variant_request:
                    duration = _hls_media_duration(text)
                    target_subtitle = self.relay.subtitle_target
                    if duration is None or target_subtitle is None:
                        body = _rewrite_hls_manifest(text, final_url, self.relay.relay_url).encode("utf-8")
                    else:
                        playlist_url = self.relay.subtitle_playlist_url_for(target_subtitle, duration)
                        body = _wrap_hls_media_playlist(
                            self.relay.variant_url_for(target),
                            playlist_url,
                            subtitle_language=self.relay.subtitle_language,
                            subtitle_label=self.relay.subtitle_label,
                        ).encode("utf-8")
                        self.relay._debug_log(
                            f"[android-relay] video {method} hls-media-wrapped -> {status} "
                            "application/vnd.apple.mpegurl"
                        )
                else:
                    body = _rewrite_hls_manifest(text, final_url, self.relay.relay_url).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type or "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(body)
                return

            if not is_hls:
                self.relay._debug_log(
                    f"[android-relay] video {method} segment -> {status} "
                    f"{content_type.split(';', 1)[0] or 'application/octet-stream'}"
                )

            self.send_response(status)
            passthrough = (
                "Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
                "ETag", "Last-Modified", "Cache-Control",
            )
            for name in passthrough:
                value = upstream.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if not send_body:
                return
            while True:
                chunk = upstream.read(128 * 1024)
                if not chunk:
                    break
                self.relay.last_activity = time.monotonic()
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            upstream.close()


def run_android_relay(config_path: str) -> int:
    config_file = Path(config_path)
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return 2
    try:
        config_file.unlink()
    except OSError:
        pass

    token = str(config.get("token") or "")
    ready = Path(str(config.get("ready") or ""))
    if not token or not str(ready):
        return 2
    referer = str(config.get("referer") or "")
    initial_target = str(config.get("initial_target") or "") or None
    subtitle_target = str(config.get("subtitle_target") or "") or None
    subtitle_language = str(config.get("subtitle_language") or "") or None
    subtitle_label = str(config.get("subtitle_label") or "") or None
    user_agent = str(config.get("user_agent") or USER_AGENT)
    idle_timeout = max(60.0, float(config.get("idle_timeout") or 3600))
    debug = bool(config.get("debug") or False)
    log_file = str(config.get("log_file") or "") or None

    server = _AndroidRelayHTTPServer(
        ("127.0.0.1", 0), _AndroidRelayHandler,
        token=token, referer=referer, user_agent=user_agent,
        debug=debug,
        log_path=log_file,
        subtitle_target=subtitle_target,
        subtitle_language=subtitle_language,
        subtitle_label=subtitle_label,
        allowed_targets=[target for target in (initial_target, subtitle_target) if target],
    )

    def request_shutdown(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, request_shutdown)
    server.timeout = 1.0
    server._debug_log("[android-relay] started")
    ready.write_text(json.dumps({"port": server.server_address[1], "token": token}), encoding="utf-8")
    try:
        while time.monotonic() - server.last_activity < idle_timeout:
            server.handle_request()
    finally:
        server._debug_log("[android-relay] stopped")
        server.server_close()
        try:
            ready.unlink()
        except OSError:
            pass
    return 0


@dataclasses.dataclass(frozen=True)
class AndroidRelayEndpoint:
    port: int
    token: str

    def url_for(self, target: str) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/{_relay_encode(target)}"

    def subtitle_url_for(self, target: str) -> str:
        # VLC for Android keys subtitle handling off the resource name/type,
        # so the relayed subtitle URL ends in a cosmetic subtitle suffix.
        # The suffix is route-only; the upstream URL is never mutated.
        suffix = _subtitle_suffix_for(target)
        return f"http://127.0.0.1:{self.port}/{self.token}/subtitle/{_relay_encode(target)}{suffix}"


# ---------- player / downloader ----------

class _IpcSession:
    """One persistent mpv IPC connection carrying both replies and events.

    mpv's input-ipc-server accepts a single client at a time and pushes
    unsolicited events (notably ``end-file``) only to a client that happens to
    be connected when they fire. A connect/disconnect-per-command client
    therefore silently drops every event in between, which is why polling
    ``eof-reached`` was both slow to notice EOF and prone to reading a stale
    ``True`` from the file that just finished. One connection is held for the
    whole episode and multiplexes replies (matched by ``request_id``) with
    events.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cv = threading.Condition()
        self._sock: Optional[socket.socket] = None
        self._replies: dict[int, Optional[dict]] = {}
        self._next_id = 1
        self._end_seq = 0
        self._end_reason: Optional[str] = None
        self._closed = False

    def open(self) -> bool:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(str(self.path))
        except OSError:
            sock.close()
            return False
        self._sock = sock
        threading.Thread(target=self._read_loop, daemon=True).start()
        return True

    def _read_loop(self) -> None:
        buf = b""
        while not self._closed:
            sock = self._sock
            if sock is None:
                break
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                with self._cv:
                    if "event" in msg:
                        if msg.get("event") == "end-file":
                            self._end_reason = msg.get("reason")
                            self._end_seq += 1
                    elif "request_id" in msg:
                        self._replies[msg["request_id"]] = msg
                    self._cv.notify_all()
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def command(self, command: list[object], timeout: float = 2.0) -> object:
        sock = self._sock
        if sock is None:
            raise RuntimeError("mpv IPC session is not open")
        with self._cv:
            if self._closed:
                raise RuntimeError("mpv IPC session is closed")
            rid = self._next_id
            self._next_id += 1
            self._replies[rid] = None
        try:
            sock.sendall((json.dumps({"command": command, "request_id": rid}) + "\n").encode("utf-8"))
        except OSError:
            with self._cv:
                self._replies.pop(rid, None)
            raise
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                reply = self._replies.get(rid)
                if reply is not None:
                    self._replies.pop(rid, None)
                    if reply.get("error") != "success":
                        raise RuntimeError(str(reply.get("error")))
                    return reply.get("data")
                if self._closed:
                    self._replies.pop(rid, None)
                    raise RuntimeError("mpv IPC connection closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._replies.pop(rid, None)
                    raise RuntimeError("no reply from mpv")
                self._cv.wait(remaining)

    def end_file_seq(self) -> int:
        with self._cv:
            return self._end_seq

    def is_closed(self) -> bool:
        with self._cv:
            return self._closed

    def wait_end_file(self, after: int, timeout: float) -> Optional[str]:
        """Wait for an end-file event newer than the `after` sequence."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if self._end_seq > after:
                    return self._end_reason
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    def close(self) -> None:
        with self._cv:
            self._closed = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        with self._cv:
            self._cv.notify_all()


class PlayKw(TypedDict):
    """Exact keyword set shared by Playback.play and Playback.replace.

    Naming the keys keeps `play(**kwargs)` precisely checkable; an untyped dict
    expands as "any string key is possible", which is neither true nor useful.
    """

    title: str
    subtitle: Optional[str]
    referer: str
    mal_id: Optional[str]
    episode: str
    subtitle_language: Optional[str]
    subtitle_label: Optional[str]


class SkipData(TypedDict, total=False):
    """Parsed view of the flags ani-skip emits; keys are optional.

    Parsed once, on the Python side, out of ani-skip's `--chapters-file`
    (for the OSC scrub bar) and its `script-opts` (the skip intervals we
    feed to the embedded skip runtime).
    """

    chapters_file: str
    op_start: float
    op_end: float
    ed_start: float
    ed_end: float
    offset: float


_SKIP_CHAPTERS_RE = re.compile(r"--chapters-file=(\S+)")
_SKIP_OP_START_RE = re.compile(r"skip-op_start=([0-9.]+)")
_SKIP_OP_END_RE = re.compile(r"skip-op_end=([0-9.]+)")
_SKIP_ED_START_RE = re.compile(r"skip-ed_start=([0-9.]+)")
_SKIP_ED_END_RE = re.compile(r"skip-ed_end=([0-9.]+)")
_SKIP_OFFSET_RE = re.compile(r"skip-offset=([0-9.]+)")


def parse_skip_flags(flags: Sequence[str]) -> Optional[SkipData]:
    if not flags:
        return None
    text = " ".join(flags)
    result: SkipData = {}
    chapters_match = _SKIP_CHAPTERS_RE.search(text)
    if chapters_match:
        result["chapters_file"] = chapters_match.group(1)
    for name, pattern in (
        ("op_start", _SKIP_OP_START_RE),
        ("op_end", _SKIP_OP_END_RE),
        ("ed_start", _SKIP_ED_START_RE),
        ("ed_end", _SKIP_ED_END_RE),
        ("offset", _SKIP_OFFSET_RE),
    ):
        match = pattern.search(text)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if name == "op_start":
            result["op_start"] = value
        elif name == "op_end":
            result["op_end"] = value
        elif name == "ed_start":
            result["ed_start"] = value
        elif name == "ed_end":
            result["ed_end"] = value
        else:
            result["offset"] = value
    return result or None


# Embedded into the standalone build and written to the state directory on
# first use of --skip. It is the *same* skip logic the user's
# ~/.config/mpv/scripts/skip.lua implements, except it re-reads the
# interval file ani-py owns on every file-loaded event -- see CONTEXT.md
# under "--skip". The user's skip.lua keeps skipping nothing as long as
# ani-py --skip is active (it receives no script-opts anymore).
ANI_PY_SKIP_LUA = r'''
local mpv = require("mp")

local function read_state()
    local path = os.getenv("ANI_PY_SKIP_FILE")
    if not path or path == "" then
        return nil
    end
    local f = io.open(path, "r")
    if not f then
        return nil
    end
    local values = {}
    for line in f:lines() do
        -- Lua's %w covers letters and digits only, so "op_start" etc. need %w_.
        local key, value = line:match("^([%w_]+)=(.+)$")
        if key then
            values[key] = tonumber(value) or 0
        end
    end
    f:close()
    if values.op_start then
        return values
    end
    return nil
end

local intervals = { op_start = 0, op_end = 0, ed_start = 0, ed_end = 0, offset = 0 }
local skipped_op = false
local skipped_ed = false

local function apply_state()
    local current = read_state()
    if current then
        intervals.op_start = current.op_start or 0
        intervals.op_end = current.op_end or 0
        intervals.ed_start = current.ed_start or 0
        intervals.ed_end = current.ed_end or 0
        intervals.offset = current.offset or 0
    else
        intervals.op_start, intervals.op_end, intervals.ed_start,
            intervals.ed_end, intervals.offset = 0, 0, 0, 0, 0
    end
    skipped_op = false
    skipped_ed = false
end

local function check()
    local t = mp.get_property_number("time-pos")
    if not t then
        return
    end
    local op_target = intervals.op_end - intervals.offset
    local ed_target = intervals.ed_end - intervals.offset
    if t >= intervals.op_start and t < op_target then
        if not skipped_op then
            mp.set_property_number("time-pos", op_target)
            skipped_op = true
        end
    end
    if t >= intervals.ed_start and t < ed_target then
        if not skipped_ed then
            mp.set_property_number("time-pos", ed_target)
            skipped_ed = true
        end
    end
end

mp.observe_property("time-pos", "number", check)
mp.register_event("file-loaded", apply_state)
'''


class Playback:
    """Launch players and own one private mpv IPC endpoint.

    The private endpoint is intentional: a user's mpv.conf may define a global
    socket such as /tmp/mpvsocket that other tools depend on.  Passing our own
    --input-ipc-server on the command line prevents ani-py from hijacking or
    colliding with that shared socket while still loading the rest of mpv.conf,
    input.conf and user scripts normally.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.player = self._detect_player()
        self.proc: Optional[subprocess.Popen] = None
        self.ipc_path: Optional[Path] = None
        self._session: Optional[_IpcSession] = None
        # Auto-next needs mpv to end files instead of pausing on them, so that
        # end-file events are emitted and can be told apart from failures.
        self.event_completion = False
        self._detached = False
        self._android_launched = False
        self._android_relay_proc: Optional[subprocess.Popen] = None
        self._android_relay_dir: Optional[Path] = None
        # ani-skip flags are per (mal id, episode) and cost a subprocess call,
        # so remember them; _skip_data turns the cached flags into the chapters
        # option plus the skip-state file the embedded mpv script reads.
        self._skip_cache: dict[tuple[Optional[str], str], list[str]] = {}
        # Remembered only when we actually captured it: never invent defaults
        # for a window this run has not seen.
        self._window_state: dict[str, object] = WindowStateStore().load()

    def _detect_player(self) -> str:
        if self.args.download:
            return "download"

        # --player/-p is the single player selector on every platform.
        # Android apps are launched by VIEW intents; do not preflight packages
        # with `pm path`, which is unreliable from an ordinary Termux UID.
        requested_player = (self.args.player or "").strip()
        if is_android_environment():
            requested = requested_player or "auto"
            requested = requested.lower()
            if requested not in {"auto", "vlc", "mpv"}:
                fail("On Termux/Android, --player supports: auto, vlc, mpv.")
            return f"android_{requested}"

        if requested_player and requested_player.lower() != "auto":
            resolved = which_first([requested_player])
            if not resolved:
                fail(f"Requested player '{requested_player}' was not found.")
            return resolved

        system = platform.system()
        if system == "Darwin":
            resolved = which_first(["iina", "/Applications/IINA.app/Contents/MacOS/iina-cli", "mpv", "vlc"])
        elif system == "Windows":
            resolved = which_first(["mpv.exe", "vlc.exe"])
        else:
            resolved = which_first(["mpv", "vlc"])
        if not resolved:
            fail("No media player found. Install mpv or VLC, or pass --player.")
        return resolved

    def _is_android(self) -> bool:
        return self.player in {"android_auto", "android_vlc", "android_mpv"}

    def _is_mpv(self) -> bool:
        if self.player == "download" or self._is_android():
            return False
        return "mpv" in Path(self.player).name.lower()

    def _ipc_supported(self) -> bool:
        return os.name == "posix" and hasattr(socket, "AF_UNIX")

    def auto_next_supported(self) -> bool:
        """Whether playback exposes a reliable natural-EOF signal."""
        return self._is_mpv() and self._ipc_supported()

    def reached_eof(self) -> Optional[bool]:
        """True/False if mpv could be asked, None if it cannot be reached.

        The None case matters: once the player is gone the answer is unknown,
        not false. Callers must not turn "cannot ask" into "did not finish",
        or an episode that really did finish would be recorded as unfinished.
        """
        session = self._session
        if session is None:
            return None
        try:
            value = session.command(["get_property", "eof-reached"], timeout=0.6)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return None
        # A non-boolean answer is "unknown", not "not finished": returning
        # False here would let an exit-path record overwrite a real finish.
        return value if isinstance(value, bool) else None

    def progress(self) -> Optional[tuple[float, float]]:
        """Current (position, duration) in seconds, or None when unknown.

        Duration is 0.0 for live/segmented streams that do not report one.
        """
        session = self._session
        if session is None:
            return None
        try:
            position = session.command(["get_property", "time-pos"], timeout=0.5)
            duration = session.command(["get_property", "duration"], timeout=0.5)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return None
        if not isinstance(position, (int, float)):
            return None
        span = float(duration) if isinstance(duration, (int, float)) else 0.0
        return float(position), span

    def wait_for_completion(
        self, poll_interval: float = 0.2, on_wait: Optional[Callable[[], None]] = None
    ) -> str:
        """Wait for the current mpv item to finish.

        Returns "eof" only when mpv reports that the media reached its natural
        end. A manual stop/close or player exit returns "closed", a stream that
        ended in an error returns "failed", a source that stopped delivering
        bytes returns "stalled"; a socket that stops answering while the
        process is still alive returns "ipc-lost". Callers never guess that an
        interrupted episode was completed.

        With a live IPC session this waits for mpv's ``end-file`` event, whose
        ``reason`` distinguishes a real EOF from an error, a quit, or a
        replacement. Polling ``eof-reached`` cannot make that distinction and is
        racy in three ways: the property is briefly unavailable while a file is
        swapping, it can still read ``True`` from the file that just finished
        right after a loadfile, and it becomes permanently unavailable once mpv
        unloads at the end. It remains only as a fallback for a socket ani-py
        could not hold open (an adopted detached session).
        """
        if not self.auto_next_supported() or self.ipc_path is None:
            return "unsupported"
        session = self._session
        if session is not None:
            return self._wait_end_file(session, on_wait)
        return self._wait_eof_poll(poll_interval, on_wait)

    def _wait_end_file(self, session: _IpcSession, on_wait=None) -> str:
        seq = session.end_file_seq()
        stalled_for = 0.0
        while True:
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                self.proc = None
                self._cleanup_ipc()
                return "closed"
            reason = session.wait_end_file(seq, timeout=0.5)
            if on_wait is not None:
                on_wait()
            if reason is not None:
                if reason in ("eof", "eof-explicit"):
                    return "eof"
                if reason == "error":
                    return "failed"
                return "closed"
            if session.is_closed():
                proc = self.proc
                if proc is not None and proc.poll() is not None:
                    self.proc = None
                    self._cleanup_ipc()
                    return "closed"
                self._cleanup_ipc()
                return "ipc-lost"
            # mpv retries a stalled source itself and never ends the file, so
            # end-file alone would wait forever. Its retry flaps the cache flag
            # on and off, so accumulate buffered time instead of requiring an
            # unbroken run; a healthy episode barely pauses for cache at all.
            try:
                paused = session.command(["get_property", "paused-for-cache"], timeout=0.8)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                paused = None
            # Plain truth test: None (player unreachable) and False both mean
            # "not buffering", and only a confirmed True accumulates.
            if paused:
                stalled_for += 0.5
                if stalled_for > IPC_STALL_SECONDS:
                    return "stalled"
            else:
                stalled_for = 0.0

    def _wait_eof_poll(self, poll_interval: float, on_wait=None) -> str:
        missed = 0
        while True:
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                self.proc = None
                self._cleanup_ipc()
                return "closed"
            try:
                reached = self._ipc(["get_property", "eof-reached"], timeout=0.6)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                proc = self.proc
                if proc is not None and proc.poll() is not None:
                    self.proc = None
                    self._cleanup_ipc()
                    return "closed"
                missed += 1
                if missed > IPC_GRACE_POLLS:
                    return "ipc-lost"
            else:
                missed = 0
                if reached is True:
                    return "eof"
            if on_wait is not None:
                on_wait()
            if poll_interval > 0:
                time.sleep(poll_interval)

    def _find_rish(self) -> Optional[str]:
        candidates = [
            shutil.which("rish"),
            str(Path.home() / "rish"),
            str(Path.home() / ".local/bin/rish"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    @staticmethod
    def _android_launch_ok(proc: subprocess.CompletedProcess[str]) -> bool:
        combined = f"{proc.stdout}\n{proc.stderr}".lower()
        bad = (
            "unable to resolve intent",
            "error: activity not started",
            "failure calling service",
            "permission denied",
            "securityexception",
            "exception occurred",
            "am.sock",
            "connection refused",
        )
        return proc.returncode == 0 and not any(marker in combined for marker in bad)

    def _android_intent(self, player: str, url: str, title: str, subtitle: Optional[str]) -> list[str]:
        cmd = [
            "am", "start", "--user", _android_user_id(),
            "-a", "android.intent.action.VIEW",
        ]
        if player == "vlc":
            # Package targeting is more stable than a private activity class and
            # still lets Android route VIEW to VLC's exported playback entry.
            cmd += ["-t", "video/*", "-p", "org.videolan.vlc"]
        elif player == "mpv":
            # mpv-android documents package targeting + video/any for URLs whose
            # path does not carry a recognizable media extension.
            cmd += ["-t", "video/any", "-p", "is.xyz.mpv"]
        else:
            cmd += ["-t", "video/*"]
        cmd += ["-d", url, "--es", "title", title]
        # VLC accepts this simple string extra. mpv-android's official `subs`
        # extra is ParcelableArray<Uri>, which shell `am` cannot construct.
        if subtitle and player in {"vlc", "auto"}:
            cmd += ["--es", "subtitles_location", subtitle]
        return cmd

    def _android_debug(self) -> bool:
        return bool(getattr(self.args, "android_debug", False))

    def _print_android_intent_debug(
        self, intent: list[str], subtitle_url: Optional[str], subtitle_suffix: Optional[str],
    ) -> None:
        component = "org.videolan.vlc/org.videolan.vlc.gui.video.VideoPlayerActivity"
        if "-n" in intent:
            target = "VLC VideoPlayerActivity"
        elif "org.videolan.vlc" in intent:
            target = "VLC package"
        elif "is.xyz.mpv" in intent:
            target = "mpv-android package"
        else:
            target = "Android resolver"
        print("Android intent:", file=sys.stderr)
        print(f"  target: {target}", file=sys.stderr)
        if "-n" in intent and component not in intent:
            target = "Android component"
            print(f"  target: {target}", file=sys.stderr)
        has_subtitle = "subtitles_location" in intent
        print(f"  subtitle extra: {'present' if has_subtitle else 'none'}", file=sys.stderr)
        scheme = urlsplit(subtitle_url).scheme if has_subtitle and subtitle_url else "none"
        print(f"  subtitle extra scheme: {scheme or 'none'}", file=sys.stderr)
        suffix = subtitle_suffix if has_subtitle and subtitle_suffix else "none"
        print(f"  subtitle suffix: {suffix}", file=sys.stderr)

    def _print_android_result_debug(self, proc: subprocess.CompletedProcess[str]) -> None:
        combined = proc.stdout.strip() or proc.stderr.strip() or "no output"
        first_line = next((line.strip() for line in combined.splitlines() if line.strip()), "no output")
        sanitized = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s}\]]+", "<redacted-url>", first_line)
        if len(sanitized) > 240:
            sanitized = sanitized[:237] + "..."
        print("Android intent result:", file=sys.stderr)
        print(f"  return code: {proc.returncode}", file=sys.stderr)
        print(f"  result: {sanitized}", file=sys.stderr)

    def _android_diagnostics(
        self, requested: str, relay_active: bool, subtitle: Optional[str], subtitle_suffix: Optional[str],
    ) -> None:
        # Concise pre-launch report for --android-debug. Never prints signed
        # upstream URLs, tokens, or file contents.
        player_label = {"vlc": "VLC", "mpv": "mpv-android"}.get(requested, requested)
        print("Android playback diagnostics", file=sys.stderr)
        print(f"  player: {player_label}", file=sys.stderr)
        print(f"  relay: {'active' if relay_active else 'inactive (direct URL)'}", file=sys.stderr)
        if not subtitle:
            print("  subtitle: none", file=sys.stderr)
            return
        print("  subtitle: resolved", file=sys.stderr)
        subtitle_type = (subtitle_suffix or _subtitle_suffix_for(subtitle)).lstrip('.').upper()
        print(f"  subtitle type: {subtitle_type}", file=sys.stderr)
        if relay_active:
            print("  subtitle transport: localhost relay", file=sys.stderr)
            print(f"  subtitle relay suffix: {subtitle_suffix or _subtitle_suffix_for(subtitle)}", file=sys.stderr)
            if self._android_relay_dir is not None:
                print(f"  relay log: {self._android_relay_dir / 'android-relay.log'}", file=sys.stderr)
        else:
            print("  subtitle transport: direct URL", file=sys.stderr)

    def _start_android_relay(
        self,
        referer: str,
        initial_target: str,
        subtitle: Optional[str] = None,
        subtitle_language: Optional[str] = None,
        subtitle_label: Optional[str] = None,
    ) -> Optional[AndroidRelayEndpoint]:
        # A loopback relay makes Referer-protected streams usable by Android
        # players without requiring player-specific config or Shizuku. It also
        # rewrites HLS child playlists/segments so every request keeps headers.
        self._stop_android_relay()
        root = Path(tempfile.mkdtemp(prefix="ani-py-relay-"))
        config = root / "config.json"
        ready = root / "ready.json"
        token = secrets.token_urlsafe(18)
        debug = self._android_debug()
        log_file = str(root / "android-relay.log") if debug else ""
        if debug and log_file:
            try:
                Path(log_file).write_text("", encoding="utf-8")
            except OSError:
                pass
        config.write_text(json.dumps({
            "ready": str(ready),
            "token": token,
            "referer": referer,
            "initial_target": initial_target,
            "subtitle_target": subtitle,
            "subtitle_language": subtitle_language,
            "subtitle_label": subtitle_label,
            "user_agent": USER_AGENT,
            "idle_timeout": 3600,
            "debug": debug,
            "log_file": log_file,
        }), encoding="utf-8")
        try:
            config.chmod(0o600)
        except OSError:
            pass

        cmd = [sys.executable, str(Path(__file__).resolve()), "--_android-relay-config", str(config)]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            warn(f"Could not start Android header relay ({exc}); trying the raw stream URL.")
            shutil.rmtree(root, ignore_errors=True)
            return None

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if ready.exists():
                try:
                    data = json.loads(ready.read_text(encoding="utf-8"))
                    endpoint = AndroidRelayEndpoint(port=int(data["port"]), token=str(data["token"]))
                    self._android_relay_proc = proc
                    self._android_relay_dir = root
                    return endpoint
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    pass
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        try:
            proc.terminate()
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)
        warn("Android header relay did not start; trying the raw stream URL.")
        return None

    def _stop_android_relay(self) -> None:
        proc = self._android_relay_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        self._android_relay_proc = None
        if self._android_relay_dir is not None:
            shutil.rmtree(self._android_relay_dir, ignore_errors=True)
        self._android_relay_dir = None

    def _run_android_intent(self, intent: list[str]) -> bool:
        if self._android_debug():
            subtitle_url: Optional[str] = None
            if "subtitles_location" in intent:
                subtitle_index = intent.index("subtitles_location") + 1
                subtitle_url = intent[subtitle_index] if subtitle_index < len(intent) else None
            self._print_android_intent_debug(intent, subtitle_url, _subtitle_suffix_for(subtitle_url) if subtitle_url else None)
        am = shutil.which("am")
        if am:
            direct = [am, *intent[1:]]
            proc = run_capture(direct)
            if self._android_debug():
                self._print_android_result_debug(proc)
            if self._android_launch_ok(proc):
                return True

        # rish/Shizuku is intentionally only a compatibility fallback. Most
        # Termux users should never need it; users who already have it get a
        # transparent retry of the same targeted intent.
        rish = self._find_rish()
        if rish:
            proc = run_capture([rish, "-c", shlex.join(intent)])
            if self._android_debug():
                self._print_android_result_debug(proc)
            if self._android_launch_ok(proc):
                warn("Direct Termux intent failed; launched through existing rish/Shizuku fallback.")
                return True
        return False

    def _android_chooser(self, url: str) -> bool:
        opener = shutil.which("termux-open")
        if not opener:
            return False
        proc = run_capture([opener, "--view", "--content-type", "video/*", url])
        return proc.returncode == 0

    def _play_android(
        self,
        stream: Stream,
        *,
        title: str,
        subtitle: Optional[str],
        referer: str,
        subtitle_language: Optional[str] = None,
        subtitle_label: Optional[str] = None,
    ) -> int:
        relay = (
            self._start_android_relay(
                referer,
                stream.url,
                subtitle,
                subtitle_language=subtitle_language,
                subtitle_label=subtitle_label,
            )
            if urlsplit(stream.url).scheme in {"http", "https"}
            else None
        )
        video_url = relay.url_for(stream.url) if relay else stream.url
        requested = self.player.removeprefix("android_")
        # WebVTT subtitles ride the relayed HLS playlist as a native rendition
        # for VLC and mpv-android. VLC also receives subtitles_location as a
        # fallback, without writing persistent files into shared storage.
        subtitle_url: Optional[str] = None
        subtitle_suffix: Optional[str] = None
        if subtitle:
            subtitle_url = relay.subtitle_url_for(subtitle) if relay else subtitle
            subtitle_suffix = _subtitle_suffix_for(subtitle)

        if self._android_debug():
            self._android_diagnostics(requested, relay is not None, subtitle_url or subtitle, subtitle_suffix)
        # `auto` intentionally asks Android first instead of querying packages.
        # This supports VLC, mpv-android, MX Player, Just Player, etc. without
        # brittle `pm path` checks. Explicit vlc/mpv modes pin a package.
        if requested == "auto":
            intent = self._android_intent("auto", video_url, title, subtitle_url)
            if self._run_android_intent(intent):
                self._android_launched = True
                return 0
            if self._android_chooser(video_url):
                self._android_launched = True
                return 0
            # If implicit dispatch failed, targeted retries can still help when
            # a device's resolver behaves oddly.
            for candidate in ("vlc", "mpv"):
                if self._run_android_intent(self._android_intent(candidate, video_url, title, subtitle_url)):
                    self._android_launched = True
                    return 0
        else:
            intent = self._android_intent(requested, video_url, title, subtitle_url)
            if self._run_android_intent(intent):
                self._android_launched = True
                return 0
            # Preserve explicit choice as long as possible; only then offer the
            # ordinary Android chooser rather than requiring Shizuku setup.
            if self._android_chooser(video_url):
                warn(f"Could not target {requested}; opened Android's video-player chooser instead.")
                self._android_launched = True
                return 0

        self._stop_android_relay()
        fail(
            "Could not launch an Android video player. Install termux-tools/termux-am and a video player "
            "(VLC or mpv-android). rish/Shizuku is supported only as an optional fallback."
        )
        return 1

    def _skip_args(self, mal_id: Optional[str], episode: str) -> list[str]:
        """Return episode-specific mpv flags from ani-skip.

        Current ani-skip accepts a known MyAnimeList id directly with -i/--id.
        A missing MAL id or a failing ani-skip invocation should never fail
        playback, but it must be visible instead of silently disabling --skip.

        Results are cached per episode: replace() asks whether the next episode
        produced flags before choosing an in-place or fresh process, and
        _mpv_command needs the same answer again.
        """
        if not self.args.skip:
            return []
        key = (mal_id, episode)
        if key in self._skip_cache:
            return list(self._skip_cache[key])
        flags = self._fetch_skip_args(mal_id, episode)
        self._skip_cache[key] = flags
        return list(flags)

    def _fetch_skip_args(self, mal_id: Optional[str], episode: str) -> list[str]:
        if not mal_id:
            warn("--skip requested, but the provider did not expose a MAL id for this episode.")
            return []
        exe = shutil.which("ani-skip")
        if not exe:
            warn("--skip requested, but ani-skip is not installed.")
            return []
        # Providers already give us the MAL id, so use ani-skip's direct-id
        # interface instead of sending a numeric id through the query path.
        command = [exe, "-i", mal_id, "-e", episode]
        proc = run_capture(command)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            warn(f"ani-skip failed for episode {episode}: {detail}")
            return []
        flags = split_flags(proc.stdout.strip())
        if not flags:
            warn(f"ani-skip returned no mpv flags for episode {episode}.")
        return flags

    def _skip_state_path(self) -> Path:
        if self.ipc_path is not None:
            return self.ipc_path.with_suffix(".skip")
        return Path(tempfile.gettempdir()) / f"ani-py-skip-{os.getpid()}.state"

    def _ensure_skip_script(self) -> Optional[Path]:
        if not self._is_mpv() or self._is_android():
            return None
        root = Path(os.getenv("ANI_PY_HIST_DIR") or os.getenv("XDG_STATE_HOME") or (Path.home() / ".local/state"))
        directory = root / APP_NAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            script = directory / "ani-py-skip.lua"
            if not script.exists() or script.read_text(encoding="utf-8", errors="replace") != ANI_PY_SKIP_LUA:
                script.write_text(ANI_PY_SKIP_LUA, encoding="utf-8")
            return script
        except OSError as exc:
            warn(f"Could not stage the ani-py skip script ({exc}); falling back to restarts.")
            return None

    def _write_skip_state(self, mal_id: Optional[str], episode: str) -> Optional[Path]:
        path = self._skip_state_path()
        data = self._skip_data(mal_id, episode) or {}
        values: dict[str, float] = {}
        for key in ("op_start", "op_end", "ed_start", "ed_end", "offset"):
            v = data.get(key)
            values[key] = float(v) if isinstance(v, (int, float)) else 0.0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            warn(f"Could not write the skip state file ({exc}); skip intervals are unavailable this episode.")
            return None
        return path

    def _skip_data(self, mal_id: Optional[str], episode: str) -> Optional[SkipData]:
        if not self.args.skip:
            return None
        return parse_skip_flags(self._skip_args(mal_id, episode))

    def _window_flags(self) -> list[str]:
        state = self._window_state
        if not state:
            return []
        flags: list[str] = []
        if state.get("fullscreen") is True:
            flags.append("--fullscreen")
        geometry = state.get("geometry")
        if isinstance(geometry, str) and _WINDOW_GEOMETRY_RE.match(geometry) is not None:
            flags.append(f"--geometry={geometry}")
        autofit = state.get("autofit")
        if isinstance(autofit, str) and _WINDOW_AUTOFIT_RE.match(autofit) is not None:
            flags.append(f"--autofit={autofit}")
        return flags

    def _capture_window_state(self) -> None:
        """Snapshot the playing window's shape before a restart replaces it.

        Runs synchronously before teardown so the same properties the user
        picked (fullscreen, size/placement, fit) can be offered back to the
        next launch instead of reverting to mpv.conf geometry.
        """
        if not self._is_mpv() or self.ipc_path is None:
            return
        captured: dict[str, object] = {}
        try:
            fullscreen = self._ipc(["get_property", "fullscreen"], timeout=0.5)
            if isinstance(fullscreen, bool):
                captured["fullscreen"] = fullscreen
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            pass
        for name in ("geometry", "autofit"):
            try:
                value = self._ipc(["get_property", name], timeout=0.5)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                continue
            if isinstance(value, str) and _WINDOW_GEOMETRY_RE.match(value) is not None:
                captured[name] = value

        if captured:
            WindowStateStore().save(captured)
            self._window_state = dict(captured)

    def _make_ipc_path(self) -> Path:
        # Explicit opt-in can be used to share a socket, but the default must be
        # private so tools such as yt-cli/mpv-control can keep /tmp/mpvsocket.
        explicit = self.args.ipc_socket or os.getenv("ANI_PY_IPC_SOCKET")
        if explicit:
            return Path(explicit).expanduser()
        root = Path(os.getenv("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        uid = os.getuid() if hasattr(os, "getuid") else "user"
        return root / f"ani-py-{uid}-{os.getpid()}.sock"

    def _ipc(self, command: list[object], timeout: float = 2.0) -> object:
        session = self._session
        if session is not None:
            return session.command(command, timeout)
        if self.ipc_path is None:
            raise RuntimeError("mpv IPC is not active")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(self.ipc_path))
            payload = json.dumps({"command": command}).encode("utf-8") + b"\n"
            sock.sendall(payload)
            buf = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                    if "error" not in msg:
                        continue
                    if msg.get("error") != "success":
                        raise RuntimeError(str(msg.get("error")))
                    return msg.get("data")
            raise RuntimeError("no reply from mpv")
        finally:
            sock.close()

    def _open_ipc_session(self) -> bool:
        """Hold mpv's one allowed IPC connection open for event delivery."""
        if not self._ipc_supported() or self.ipc_path is None or self._session is not None:
            return False
        session = _IpcSession(self.ipc_path)
        if not session.open():
            return False
        self._session = session
        return True

    def _wait_ipc(self, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                self._ipc(["get_property", "path"], timeout=0.35)
                return True
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                time.sleep(0.08)
        return False

    def _wait_loaded(self, timeout: float = 5.0) -> bool:
        """Wait until mpv has a file loaded, not merely a live socket.

        mpv answers `get_property path` with a null value (and no error) before
        the first file is opened, so _wait_ipc succeeding does not mean
        playback state is readable yet.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                if self._ipc(["get_property", "path"], timeout=0.5):
                    return True
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                pass
            time.sleep(0.08)
        return False

    def _reset_resumed_position(self, tolerance: float = 1.0) -> bool:
        """Seek back to the start when mpv restored a finished position.

        With `save-position-on-quit` in the user's mpv config, relaunching a
        file already watched to the end resumes at the end, so mpv reports
        natural EOF immediately. That is a real EOF, but for --auto-next it
        means the whole queue is skipped in under a second. Rewinding keeps the
        episode watchable; a genuine in-progress resume is left alone.
        """
        if not self._is_mpv() or self.ipc_path is None:
            return False
        try:
            position = self._ipc(["get_property", "time-pos"], timeout=0.6)
            duration = self._ipc(["get_property", "duration"], timeout=0.6)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False
        if not isinstance(position, (int, float)) or not isinstance(duration, (int, float)):
            return False
        if duration <= 0 or position < duration - tolerance:
            return False
        try:
            self._ipc(["set_property", "time-pos", 0.0], timeout=0.6)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False
        return True

    def _clear_external_subtitles(self) -> None:
        """Drop external subtitle tracks left behind by the previous episode.

        Embedded tracks belong to the file mpv is now playing and stay; only
        ani-py-added external tracks are removed so a new episode cannot fall
        back to the last one's subtitles.
        """
        try:
            tracks = self._ipc(["get_property", "track-list"], timeout=0.6)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return
        if not isinstance(tracks, list):
            return
        ids: list[int] = []
        for track in tracks:
            if not isinstance(track, dict):
                continue
            if track.get("type") != "sub" or not track.get("external"):
                continue
            track_id = track.get("id")
            if isinstance(track_id, int):
                ids.append(track_id)
        for track_id in sorted(ids, reverse=True):
            try:
                self._ipc(["sub-remove", track_id], timeout=0.6)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                pass

    def _wait_path(self, expected: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                current = self._ipc(["get_property", "path"], timeout=0.5)
                if current == expected:
                    return True
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                pass
            time.sleep(0.08)
        return False

    def active(self) -> bool:
        if self._is_android():
            relay_alive = self._android_relay_proc is None or self._android_relay_proc.poll() is None
            return self._android_launched and relay_alive
        if not self._is_mpv() or self.ipc_path is None:
            return self.proc is not None and self.proc.poll() is None
        # `path` is unavailable while mpv is idle with nothing loaded, which is
        # exactly the state auto-next sits in between episodes, so probe a
        # property that always exists instead of concluding mpv is gone.
        try:
            self._ipc(["get_property", "idle-active"], timeout=0.4)
            return True
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False

    def adopt_detached_session(self, session: DetachedSession) -> bool:
        """Adopt an existing ani-py mpv IPC socket without restarting playback."""
        if is_android_environment():
            return False
        self.player = session.player
        self.proc = None
        self.ipc_path = Path(session.socket).expanduser()
        self._detached = False
        if not self._is_mpv() or not self.active():
            self.ipc_path = None
            return False
        return True

    def current_path(self) -> Optional[str]:
        if not self._is_mpv() or self.ipc_path is None:
            return None
        try:
            value = self._ipc(["get_property", "path"], timeout=0.5)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, str) and value else None

    def reattachable(self) -> bool:
        return not self._is_android() and self._is_mpv() and self.ipc_path is not None and self.active()

    def _kill_process_group(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            self.proc = None
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=2)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass
        self.proc = None

    def stop(self) -> None:
        self._detached = False
        if self._is_android():
            # Android intent players are external apps; killing the localhost
            # relay is the least invasive way to stop an ani-py-proxied stream.
            self._stop_android_relay()
            self._android_launched = False
            return
        if self._is_mpv() and self.ipc_path is not None:
            # Capture before quitting: the window belongs to the old mpv
            # process and is gone the moment quit lands.
            self._capture_window_state()
            try:
                self._ipc(["quit"], timeout=0.7)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
                pass
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._kill_process_group()
        else:
            self._kill_process_group()
        self.proc = None
        self._cleanup_ipc()

    def detach(self) -> None:
        # Leave the player alive. Android's relay is a detached child process
        # with an idle timeout, so playback survives ani-py exiting.
        self._detached = True
        self.proc = None

    def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.close()

    def _cleanup_ipc(self) -> None:
        self._close_session()
        path = self.ipc_path
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self.ipc_path = None

    def _mpv_command(
        self,
        stream: Stream,
        *,
        title: str,
        subtitle: Optional[str],
        referer: str,
        mal_id: Optional[str],
        episode: str,
        keep_open: bool = True,
    ) -> list[str]:
        extra = split_flags(os.getenv("ANI_PY_PLAYER_FLAGS", "")) + self.args.player_flag
        ipc_args: list[str] = []
        if self._ipc_supported():
            self.ipc_path = self._make_ipc_path()
            explicit_ipc = bool(self.args.ipc_socket or os.getenv("ANI_PY_IPC_SOCKET"))
            if self.ipc_path.exists():
                in_use = False
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.25)
                try:
                    probe.connect(str(self.ipc_path))
                    in_use = True
                except OSError:
                    pass
                finally:
                    probe.close()
                if in_use and explicit_ipc:
                    fail(
                        f"IPC socket is already in use: {self.ipc_path}. "
                        "Choose another --ipc-socket or omit it for a private socket."
                    )
                if not in_use:
                    try:
                        self.ipc_path.unlink()
                    except OSError:
                        pass
            ipc_args = [f"--input-ipc-server={self.ipc_path}"]
        else:
            self.ipc_path = None
        # Auto-next waits for mpv's end-file event, which mpv only emits when
        # it actually ends a file. With --keep-open=yes it just pauses at the
        # end instead, so the event never arrives; --idle=yes keeps the process
        # alive to load the next episode into.
        if self.event_completion:
            keep_open = False
        cmd = [
            self.player,
            *ipc_args,
            f"--keep-open={'yes' if keep_open else 'no'}",
            *(["--idle=yes"] if self.event_completion else []),
            "--video=auto",
            "--vid=auto",
            f"--referrer={referer}",
            f"--force-media-title={title}",
            # Some HLS hosts serve MPEG-TS segments under decoy extensions
            # (.jpg, .html, ...); ffmpeg's hls demuxer rejects those unless its
            # extension allowlist is widened, and only the demuxer option works.
            "--demuxer-lavf-o=allowed_extensions=ALL",
        ]
        if subtitle:
            cmd.append(f"--sub-file={subtitle}")
        # Window state is applied as CLI flags so a remembered fullscreen or
        # geometry beats mpv.conf on a fresh launch. The later user flags
        # (ANI_PY_PLAYER_FLAGS / --player-flag) still win because mpv
        # honours the last occurrence of an option.
        cmd += self._window_flags()
        if self.args.skip:
            # With the bundled skip script the user's skip.lua namespace
            # stays empty, so the script-opts path would double-apply.
            script = self._ensure_skip_script() if self._is_mpv() else None
            if script is not None:
                os.environ["ANI_PY_SKIP_FILE"] = str(self._skip_state_path())
                self._write_skip_state(mal_id, episode)
                data = self._skip_data(mal_id, episode) or {}
                cmd.append(f"--script={script}")
                chapters_file = data.get("chapters_file")
                if isinstance(chapters_file, str) and chapters_file:
                    cmd.append(f"--chapters-file={chapters_file}")
            else:
                cmd += self._skip_args(mal_id, episode)
        cmd += extra + [stream.url]
        return cmd

    def play(
        self,
        stream: Stream,
        *,
        title: str,
        subtitle: Optional[str],
        referer: str,
        mal_id: Optional[str],
        episode: str,
        subtitle_language: Optional[str] = None,
        subtitle_label: Optional[str] = None,
        foreground: bool = False,
        keep_open: bool = True,
    ) -> int:
        if self.player == "download":
            return self.download(
                stream,
                title=title,
                subtitle=subtitle,
                referer=referer,
                subtitle_language=subtitle_language,
                subtitle_label=subtitle_label,
            )

        extra = split_flags(os.getenv("ANI_PY_PLAYER_FLAGS", "")) + self.args.player_flag
        basename = self.player if self._is_android() else Path(self.player).name.lower()

        if self._is_android():
            if self.args.skip:
                warn("--skip is not available through Android intent players; playback will continue normally.")
            return self._play_android(
                stream,
                title=title,
                subtitle=subtitle,
                referer=referer,
                subtitle_language=subtitle_language,
                subtitle_label=subtitle_label,
            )

        if "mpv" in basename:
            # Never leave two ani-py-owned mpv instances around.
            if self.active():
                self.stop()
            # A session left over from a player that already exited would keep
            # every later _ipc call pointed at a dead socket.
            self._close_session()
            cmd = self._mpv_command(
                stream, title=title, subtitle=subtitle, referer=referer,
                mal_id=mal_id, episode=episode, keep_open=keep_open,
            )
        elif "iina" in basename:
            if self.args.skip:
                warn("--skip is supported only with mpv; IINA playback will continue without ani-skip flags.")
            cmd = [self.player, f"--mpv-referrer={referer}", f"--mpv-force-media-title={title}", "--no-stdin"]
            if subtitle:
                escaped_subtitle = subtitle.replace(":", r"\:")
                cmd.append("--mpv-sub-files=" + escaped_subtitle)
            cmd += extra + [stream.url]
        elif "vlc" in basename:
            if self.args.skip:
                warn("--skip is supported only with mpv; VLC playback will continue without ani-skip flags.")
            cmd = [self.player, f"--http-referrer={referer}", "--play-and-exit", f"--meta-title={title}"] + extra + [stream.url]
            if subtitle:
                # VLC takes a URI here; an externally found subtitle is a local path.
                location = subtitle if _is_remote_subtitle(subtitle) else Path(subtitle).resolve().as_uri()
                cmd.append(f":input-slave={location}")
        else:
            if self.args.skip:
                warn("--skip is supported only with mpv; the selected player will ignore it.")
            cmd = [self.player] + extra + [stream.url]

        if foreground or self.args.no_detach or self.args.exit_after_play:
            rc = subprocess.run(cmd).returncode
            if "mpv" in basename:
                self._cleanup_ipc()
            return rc

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if "mpv" in basename and self.ipc_path is not None:
            if not self._wait_ipc():
                warn("mpv started, but ani-py could not connect to its private IPC socket.")
            else:
                # mpv pushes end-file only to a connected client, so hold the
                # single allowed connection open before the first file can end.
                self._open_ipc_session()
                # A live socket is not a loaded file; wait for the real thing so
                # playback state is readable and a resumed EOF position can be
                # caught before it looks like a completed episode.
                self._wait_loaded()
                if self._reset_resumed_position():
                    warn("mpv resumed a saved position at the end of this file; restarting it from the beginning.")
        return 0

    def replace(
        self,
        stream: Stream,
        *,
        title: str,
        subtitle: Optional[str],
        referer: str,
        mal_id: Optional[str],
        episode: str,
        subtitle_language: Optional[str] = None,
        subtitle_label: Optional[str] = None,
    ) -> int:
        """Replace the current mpv item in-place; restart only as a fallback."""
        # Episode changes are applied in-place to keep the window (and IPC
        # socket) alive. Skip data is rebuilt for each episode: _write_skip_state
        # overwrites the file the embedded mpv script re-reads, and chapters
        # ride the loadfile per-file options. The fallback restart path keeps
        # the same flags going in via _mpv_command's chapter argument.
        if not self._is_mpv() or not self._ipc_supported() or not self.active():
            if self.active():
                self.stop()
            return self.play(
                stream,
                title=title,
                subtitle=subtitle,
                referer=referer,
                mal_id=mal_id,
                episode=episode,
                subtitle_language=subtitle_language,
                subtitle_label=subtitle_label,
            )

        try:
            self._ipc(["set_property", "referrer", referer])
            self._ipc(["set_property", "force-media-title", title])
            if self.args.skip:
                # Episode-specific skip data: ani-py owns it via a JSON state
                # file the embedded mpv script re-reads on file-loaded, and
                # chapters via loadfile's per-file options.
                self._write_skip_state(mal_id, episode)
            chapters: Optional[str] = None
            if self.args.skip:
                data = self._skip_data(mal_id, episode) or {}
                chapters_file = data.get("chapters_file")
                if isinstance(chapters_file, str):
                    chapters = chapters_file
            if chapters is not None:
                # mpv >= 0.38 takes chapter options as the fourth loadfile
                # argument (insert index -1); older accepts options as the
                # third. Try the modern form first, then the legacy shape.
                try:
                    self._ipc(["loadfile", stream.url, "replace", -1, f"chapters-file={chapters}"])
                except RuntimeError:
                    self._ipc(["loadfile", stream.url, "replace", f"chapters-file={chapters}"])
            else:
                self._ipc(["loadfile", stream.url, "replace"])
            self._wait_path(stream.url)
            self._ipc(["set_property", "force-media-title", title])
            self._clear_external_subtitles()
            if self._reset_resumed_position():
                warn("mpv resumed a saved position at the end of this file; restarting it from the beginning.")
            if subtitle:
                self._ipc(["sub-add", subtitle, "select"])
            return 0
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            warn(f"Live mpv switch failed ({exc}); restarting the player.")
            self.stop()
            return self.play(
                stream,
                title=title,
                subtitle=subtitle,
                referer=referer,
                mal_id=mal_id,
                episode=episode,
                subtitle_language=subtitle_language,
                subtitle_label=subtitle_label,
            )

    def set_subtitle(self, track: Optional[SubtitleTrack]) -> bool:
        """Switch subtitle tracks in-place when mpv IPC is available."""
        if not self._is_mpv() or not self.active():
            return False
        try:
            if track is None:
                self._ipc(["set_property", "sid", "no"])
                return True
            # Keep previously loaded tracks available for quick switching, but
            # select the requested external subtitle immediately.
            self._ipc([
                "sub-add",
                track.url,
                "select",
                track.label or "",
                track.language or "",
            ])
            return True
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False

    def resume(self) -> bool:
        """Resume an active mpv item after keep-open pauses at EOF."""
        if not self._is_mpv() or not self.active():
            return False
        try:
            self._ipc(["set_property", "pause", False])
            return True
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False

    def replay(self) -> bool:
        if not self._is_mpv() or not self.active():
            return False
        try:
            self._ipc(["seek", 0, "absolute"])
            self._ipc(["set_property", "pause", False])
            return True
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            return False

    def download(
        self,
        stream: Stream,
        *,
        title: str,
        subtitle: Optional[str],
        referer: str,
        subtitle_language: Optional[str] = None,
        subtitle_label: Optional[str] = None,
    ) -> int:
        outdir = Path(os.getenv("ANI_PY_DOWNLOAD_DIR", ".")).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip() or "episode"

        if subtitle:
            raw_tag = subtitle_language or subtitle_label
            suffix = _subtitle_suffix_for(subtitle)
            if raw_tag:
                tag = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_tag).strip("-._") or "sub"
                subtitle_name = f"{safe}.{tag}{suffix}"
            else:
                subtitle_name = f"{safe}{suffix}"
            target = outdir / subtitle_name
            if _is_remote_subtitle(subtitle):
                sub_cmd = [
                    HttpClient().exe, "--fail", "-sS", "-L", "--max-time", "30",
                    "-A", USER_AGENT, "-e", referer, subtitle,
                    "-o", str(target),
                ]
                saved = subprocess.run(sub_cmd).returncode == 0
            else:
                # An externally found subtitle is already on disk in the cache.
                try:
                    shutil.copyfile(subtitle, target)
                    saved = True
                except OSError:
                    saved = False
            if not saved:
                warn(f"Subtitle download failed for {title}; continuing with the video download.")

        yt_dlp = shutil.which("yt-dlp")
        ffmpeg = shutil.which("ffmpeg")
        output = str(outdir / f"{safe}.mp4")
        if yt_dlp:
            cmd = [
                yt_dlp,
                "--referer", referer,
                "--user-agent", USER_AGENT,
                "--no-skip-unavailable-fragments",
                "--fragment-retries", "infinite",
                "-N", "16",
                "-o", output,
                stream.url,
            ]
        elif ffmpeg:
            cmd = [
                ffmpeg,
                "-extension_picky", "0",
                "-referer", referer,
                "-user_agent", USER_AGENT,
                "-loglevel", "error", "-stats",
                "-i", stream.url,
                "-c", "copy",
                output,
            ]
        else:
            fail("Download mode requires yt-dlp or ffmpeg.")
        return subprocess.run(cmd).returncode


# ---------- external subtitle sources ----------

_ASS_START_RE = re.compile(r"^Dialogue:\s*[^,]*,(\d+):(\d\d):(\d\d)\.(\d\d),", re.MULTILINE)
_CUE_START_RE = re.compile(r"(?:(\d+):)?(\d\d):(\d\d)[.,](\d{1,3})\s*-->")
_LATIN_NAME_RE = re.compile(r"latin|latam|419|\blat\b", re.IGNORECASE)
_PARTIAL_TRACK_NAME_RE = re.compile(r"forced|forzad|signs?\b|songs?\b|carteles|karaoke", re.IGNORECASE)
_FULL_TRACK_NAME_RE = re.compile(r"full|complet|di[aá]log", re.IGNORECASE)
_MULTI_SUB_TITLE_RE = re.compile(r"spa-la|multiple subtitle|\bmulti", re.IGNORECASE)


def subtitle_cue_starts(text: str) -> list[float]:
    """Sorted, de-duplicated cue start times in seconds of an ASS/SRT/VTT file."""
    found: set[float] = set()
    for hours, minutes, seconds, fraction in _ASS_START_RE.findall(text):
        found.add(round(int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction) / 100, 3))
    if not found:
        for hours, minutes, seconds, fraction in _CUE_START_RE.findall(text):
            found.add(round(
                int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
                + int(fraction.ljust(3, "0")) / 1000,
                3,
            ))
    return sorted(found)


def subtitle_timing_agreement(candidate: Sequence[float], reference: Sequence[float]) -> float:
    """Share of the shorter track's cue starts that land on a cue start of the other.

    Two translations of one episode are cut on the same dialogue, so most of
    their cues start together; another episode, or another cut of the same
    one, agrees only by chance. Measuring from the shorter track keeps a
    sparse one (signs only) from failing a full dialogue track; a track with
    only a handful of cues proves nothing either way and scores 0.
    """
    short, long_ = sorted((sorted(candidate), sorted(reference)), key=len)
    if len(short) < SUBTITLE_SYNC_MIN_CUES:
        return 0.0
    hits = 0
    for start in short:
        index = bisect.bisect_left(long_, start)
        for neighbour in (index - 1, index):
            if 0 <= neighbour < len(long_) and abs(long_[neighbour] - start) <= SUBTITLE_SYNC_TOLERANCE:
                hits += 1
                break
    return hits / len(short)


def subtitle_coverage(candidate: Sequence[float], reference: Sequence[float]) -> float:
    """How much of the time ``reference`` speaks in ``candidate`` speaks in too.

    Counted in slots of a couple of seconds, not in cues: a sign moved frame by
    frame gives one track hundreds of cues the other has no reason to have.
    """
    def slots(cues: Sequence[float]) -> int:
        return len({int(start // SUBTITLE_COVERAGE_SLOT) for start in cues})

    return slots(candidate) / slots(reference) if reference else 0.0


def _external_subtitle_language(info: object) -> Optional[str]:
    """Language code for the ``info`` block of an Animetosho attachment."""
    if not isinstance(info, dict):
        return None
    # ISO 639-2 codes; the alias table is the one list of languages ani-py knows.
    language = _SUBTITLE_LANGUAGE_ALIASES.get(str(info.get("lang") or "").strip().lower())
    if language == "es" and _LATIN_NAME_RE.search(str(info.get("name") or "")):
        return _LATIN_SPANISH
    return language


class SubtitleSource:
    """Finds a subtitle file outside the stream provider."""

    name = "unknown"

    def supports(self, language: str) -> bool:
        """False when ``find`` can never return ``language``; the caller then skips its own setup."""
        return True

    def find(
        self, mal_id: str, episode: str, language: str, reference: Sequence[float]
    ) -> Optional[SubtitleTrack]:
        raise NotImplementedError


class AnimetoshoSubtitles(SubtitleSource):
    """Subtitle tracks Animetosho extracted from single-file releases.

    MAL id + episode -> AniDB episode id (api.ani.zip) -> releases of that
    episode -> their attached subtitle tracks. Animetosho files other episodes
    and other cuts under the same episode id, so a track is only accepted when
    its cue timing agrees with ``reference``, the provider's own subtitle for
    the video being played. Accepted files stay in the cache directory.
    """

    name = "animetosho"

    def __init__(self, http: HttpClient, cache_dir: Optional[Path] = None) -> None:
        self.http = http
        self.dir = cache_dir or (
            Path(os.getenv("XDG_CACHE_HOME") or (Path.home() / ".cache")) / APP_NAME / "subs"
        )
        self._maps: dict[str, dict[str, int]] = {}
        self._json_memo: dict[str, object] = {}
        self._failed: set[str] = set()
        self._rejected: set[int] = set()
        self._lzma_missing = False

    def supports(self, language: str) -> bool:
        # Attachment languages are read through the alias table, so a code
        # outside it ("tl", a typo) would search every release for nothing.
        return language in _KNOWN_SUBTITLE_LANGUAGES

    def find(
        self, mal_id: str, episode: str, language: str, reference: Sequence[float]
    ) -> Optional[SubtitleTrack]:
        if self._lzma_missing or not mal_id.isdigit() or not self.supports(language):
            return None
        stem = f"{mal_id}_{re.sub(r'[^0-9A-Za-z.]+', '-', episode)}"
        cached = self._cached(stem, language)
        if cached is not None:
            return cached
        if stem in self._failed or len(reference) < SUBTITLE_SYNC_MIN_CUES:
            return None
        try:
            eid = self._anidb_episode_id(mal_id, episode)
            if eid is None:
                return None
            # One listing at a time, stopping at the first hit: the feed
            # answers a burst of requests with HTTP 429.
            for url in self._release_urls(eid):
                attachments = self._subtitles(self._json(url))
                for accepted in _acceptable_subtitle_languages(language):
                    for found, item in attachments:
                        if found != accepted:
                            continue
                        track = self._download(item, stem, accepted, reference)
                        if track is not None or self._lzma_missing:
                            return track
        except HttpError as exc:
            # The next --sub-lang entry would wait on the same host, so one
            # failure settles this episode for the rest of the run.
            self._failed.add(stem)
            warn(f"External subtitle search failed: {exc}")
        return None

    def _track(self, path: Path, language: str, label: Optional[str]) -> SubtitleTrack:
        return SubtitleTrack(url=str(path), language=language, label=label, source=self.name)

    def _cached(self, stem: str, language: str) -> Optional[SubtitleTrack]:
        for accepted in _acceptable_subtitle_languages(language):
            try:
                paths = sorted(self.dir.glob(f"{stem}_{accepted}_*"))
            except OSError:
                return None
            for path in paths:
                if path.suffix in _SUBTITLE_SUFFIXES and path.is_file():
                    return self._track(path, accepted, None)
        return None

    def _write(self, path: Path, data: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=self.dir, delete=False) as handle:
            handle.write(data)
            temp_name = handle.name
        Path(temp_name).replace(path)

    def _json(self, url: str) -> object:
        # Every entry of a --sub-lang list asks about the same releases.
        if url not in self._json_memo:
            self._json_memo[url] = self.http.get_json(url, timeout=20)
        return self._json_memo[url]

    @staticmethod
    def _read_mapping(path: Path) -> dict[str, int]:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {str(key): value for key, value in loaded.items() if isinstance(value, int)}

    def _anidb_episode_id(self, mal_id: str, episode: str) -> Optional[int]:
        mapping = self._maps.get(mal_id)
        if mapping is None:
            path = self.dir / f"map_{mal_id}.json"
            mapping = self._read_mapping(path)
            if episode not in mapping:
                # An airing show gains episodes, so a miss on disk is worth
                # one fresh request per run, never one per lookup.
                data = self.http.get_json(f"{ANIZIP_URL}?mal_id={mal_id}", timeout=20)
                episodes = data.get("episodes") if isinstance(data, dict) else None
                mapping = {}
                for number, row in (episodes.items() if isinstance(episodes, dict) else []):
                    if isinstance(row, dict) and isinstance(row.get("anidbEid"), int):
                        mapping[str(number)] = row["anidbEid"]
                try:
                    self._write(path, json.dumps(mapping).encode("utf-8"))
                except OSError:
                    pass  # the mapping still serves this run from memory
            self._maps[mal_id] = mapping
        return mapping.get(episode)

    def _release_urls(self, eid: int) -> list[str]:
        """Attachment-listing URLs of the releases worth inspecting, best first."""
        data = self._json(f"{ANIMETOSHO_FEED_URL}?eid={eid}")
        rows = [
            row for row in (data if isinstance(data, list) else [])
            # A batch has no per-file episode link here; single files only.
            if isinstance(row, dict) and row.get("num_files") == 1 and isinstance(row.get("id"), int)
        ]
        rows.sort(
            key=lambda row: (
                bool(_MULTI_SUB_TITLE_RE.search(str(row.get("title") or ""))),
                row.get("timestamp") if isinstance(row.get("timestamp"), int) else 0,
            ),
            reverse=True,
        )
        return [
            f"{ANIMETOSHO_FEED_URL}?show=torrent&id={row['id']}"
            for row in rows[:SUBTITLE_MAX_RELEASES]
        ]

    @staticmethod
    def _subtitles(detail: object) -> list[tuple[str, dict]]:
        """(language, attachment) pairs of one single-file release."""
        files = detail.get("files") if isinstance(detail, dict) else None
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            return []
        attachments = files[0].get("attachments")
        found: list[tuple[str, dict]] = []
        for item in attachments if isinstance(attachments, list) else []:
            if not isinstance(item, dict) or item.get("type") != "subtitle" or not isinstance(item.get("id"), int):
                continue
            info = item.get("info")
            language = _external_subtitle_language(info)
            if not language:
                continue
            name = str(info.get("name") or "")
            if info.get("forced") or (_PARTIAL_TRACK_NAME_RE.search(name) and not _FULL_TRACK_NAME_RE.search(name)):
                # Signs and songs only: in sync with the dialogue, translating none of it.
                continue
            found.append((language, item))
        return found

    def _download(
        self, item: dict, stem: str, language: str, reference: Sequence[float]
    ) -> Optional[SubtitleTrack]:
        ident = item["id"]
        if ident in self._rejected:
            return None
        raw = self.http.get_bytes(f"{ANIMETOSHO_ATTACH_URL}/{ident:08x}/{ident}.xz", timeout=20)
        text = self._decompress(raw)
        cues = subtitle_cue_starts(text) if text is not None else []
        if (
            text is None
            or subtitle_coverage(cues, reference) < SUBTITLE_MIN_COVERAGE
            or subtitle_timing_agreement(cues, reference) < SUBTITLE_SYNC_MINIMUM
        ):
            # Unreadable, another episode filed under this id, a cut this
            # video is not, or a track that leaves most of the dialogue out.
            # The next list entry must not fetch it again.
            if not self._lzma_missing:
                self._rejected.add(ident)
            return None
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        suffix = {"ass": ".ass", "srt": ".srt"}.get(str(info.get("codec") or "").lower())
        if suffix is None:
            suffix = ".ass" if "[Script Info]" in text else ".srt"
        path = self.dir / f"{stem}_{language}_{ident}{suffix}"
        try:
            self._write(path, text.encode("utf-8"))
        except OSError as exc:
            warn(f"Could not store the external subtitle ({exc}).")
            return None
        name = info.get("name")
        return self._track(path, language, name.strip() if isinstance(name, str) and name.strip() else None)

    def _decompress(self, raw: bytes) -> Optional[str]:
        try:
            # Optional in CPython builds; everything else in ani-py works without it.
            import lzma
        except ImportError:
            if not self._lzma_missing:
                warn("This Python has no lzma module; external subtitle search is disabled.")
            self._lzma_missing = True
            return None
        decompressor = lzma.LZMADecompressor()
        try:
            data = decompressor.decompress(raw, max_length=SUBTITLE_MAX_BYTES + 1)
        except (lzma.LZMAError, EOFError):
            return None
        # No end marker: the stream was cut short or holds more than the cap.
        if not decompressor.eof or not data or len(data) > SUBTITLE_MAX_BYTES:
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if "[Script Info]" not in text and "-->" not in text:
            return None
        return text


# ---------- selection helpers ----------

def stream_rank(stream: Stream) -> int:
    m = re.match(r"(\d+)", stream.quality)
    return int(m.group(1)) if m else 0


def choose_quality(streams: Sequence[Stream], requested: str) -> Stream:
    if not streams:
        fail("No streams available.")
    req = requested.lower().rstrip("p")
    if req == "best":
        return max(streams, key=stream_rank)
    if req == "worst":
        positive = [s for s in streams if stream_rank(s)]
        return min(positive, key=stream_rank) if positive else streams[-1]
    wanted = re.sub(r"\D", "", req)
    if wanted:
        for stream in streams:
            if re.sub(r"\D", "", stream.quality) == wanted:
                return stream
        warn(f"Quality {requested} not found; using best.")
    return max(streams, key=stream_rank)


_SUBTITLE_OFF = frozenset({"off", "none", "no", "false", "0"})
_SUBTITLE_AUTO = frozenset({"", "auto", "default"})


def _split_subtitle_preference(preference: Optional[str]) -> list[str]:
    raw = (preference or "").strip()
    # A label may itself contain commas, so that form is always one entry.
    if raw.casefold().startswith("label:"):
        return [raw]
    return [part.strip() for part in raw.split(",") if part.strip()] or ["auto"]


def subtitle_preference_error(preference: Optional[str]) -> Optional[str]:
    """Why a --sub-lang value is invalid, or None when it is fine."""
    entries = _split_subtitle_preference(preference)
    for index, entry in enumerate(entries):
        folded = entry.casefold()
        if folded in _SUBTITLE_OFF and len(entries) > 1:
            return f"--sub-lang: {entry!r} turns subtitles off and cannot be combined with other values."
        if folded in _SUBTITLE_AUTO and index != len(entries) - 1:
            return f"--sub-lang: {entry!r} is only valid as the last value."
    return None


def parse_subtitle_preference(preference: Optional[str]) -> list[str]:
    """Ordered --sub-lang entries.

    Lenient where subtitle_preference_error is strict: a preference restored
    from a detached session must never be able to stop playback.
    """
    entries = _split_subtitle_preference(preference)
    if len(entries) == 1:
        return entries
    last = len(entries) - 1
    kept = [
        entry for index, entry in enumerate(entries)
        if entry.casefold() not in _SUBTITLE_OFF
        and (entry.casefold() not in _SUBTITLE_AUTO or index == last)
    ]
    return kept or ["auto"]


def choose_subtitle_track(bundle: StreamBundle, preference: Optional[str]) -> Optional[SubtitleTrack]:
    tracks = bundle.subtitle_tracks()
    if not tracks:
        return None

    pref = (preference or "auto").strip()
    folded = pref.casefold()
    if folded in _SUBTITLE_OFF:
        return None

    if folded.startswith("label:"):
        wanted = folded.split(":", 1)[1].strip()
        exact = next((track for track in tracks if (track.label or "").casefold() == wanted), None)
        if exact:
            return exact

    if folded not in _SUBTITLE_AUTO:
        language = normalize_subtitle_language(pref)
        if language:
            exact_language = _subtitle_language_matches(language, tracks)
            if exact_language:
                return exact_language
        exact_label = next((track for track in tracks if (track.label or "").casefold() == folded), None)
        if exact_label:
            return exact_label
        if language in _KNOWN_SUBTITLE_LANGUAGES:
            # "es" is inside "Portuguese" and "en" inside "French".
            return None
        partial_label = next((track for track in tracks if folded in (track.label or "").casefold()), None)
        if partial_label:
            return partial_label
        return None

    default = next((track for track in tracks if track.default), None)
    if default:
        return default
    if bundle.subtitle:
        legacy = next((track for track in tracks if track.url == bundle.subtitle), None)
        if legacy:
            return legacy
    return next((track for track in tracks if track.language == "en"), None) or tracks[0]


SubtitleLookup = Callable[[str], Optional[SubtitleTrack]]


def resolve_subtitle(
    bundle: StreamBundle,
    preference: Optional[str],
    lookup: Optional[SubtitleLookup] = None,
) -> Optional[SubtitleTrack]:
    """Pick a subtitle for ``preference``, a comma-separated priority list.

    Every entry stays strict: it is looked for among the provider's tracks and
    then, when ``lookup`` is given, in an external source. The only fallback
    is the next entry the user wrote.
    """
    for entry in parse_subtitle_preference(preference):
        track = choose_subtitle_track(bundle, entry)
        if track is not None:
            return track
        folded = entry.casefold()
        if lookup is None or folded in _SUBTITLE_OFF or folded in _SUBTITLE_AUTO or folded.startswith("label:"):
            continue
        language = normalize_subtitle_language(entry)
        if language:
            track = lookup(language)
            if track is not None:
                return track
    return None


def subtitle_menu_rows(
    tracks: Sequence[SubtitleTrack], current: Optional[SubtitleTrack]
) -> tuple[list[str], dict[str, Optional[SubtitleTrack]]]:
    rows = ["Off"]
    mapping: dict[str, Optional[SubtitleTrack]] = {"Off": None}
    for track in tracks:
        parts = [track.name]
        if track.language and track.language.casefold() not in track.name.casefold():
            parts.append(f"[{track.language}]")
        if track.default:
            parts.append("(default)")
        if current is not None and track.url == current.url:
            parts.append("• current")
        row = " ".join(parts)
        if row in mapping:
            row = f"{row}  {len(mapping)}"
        rows.append(row)
        mapping[row] = track
    return rows, mapping


def episode_index(episodes: Sequence[Episode], number: str) -> Optional[int]:
    for i, ep in enumerate(episodes):
        if ep.number == number:
            return i
    return None


def parse_episode_spec(spec: str, episodes: Sequence[Episode]) -> list[Episode]:
    if not spec:
        return []
    numbers = [ep.number for ep in episodes]
    if spec == "0":
        return [episodes[0]] if episodes else []
    if spec == "-1":
        return [episodes[-1]] if episodes else []
    if spec in numbers:
        return [episodes[numbers.index(spec)]]

    # Accept 2-5, 2:5, 2..5; episode identifiers may themselves be decimal.
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(?:-|:|\.\.)\s*(-?\d+(?:\.\d+)?)\s*", spec)
    if m:
        start, end = m.groups()
        start = numbers[0] if start == "0" else numbers[-1] if start == "-1" else start
        end = numbers[-1] if end == "-1" else numbers[0] if end == "0" else end
        if start in numbers and end in numbers:
            a, b = numbers.index(start), numbers.index(end)
            step = 1 if a <= b else -1
            return [episodes[i] for i in range(a, b + step, step)]
    return []


def format_anime_rows(anime: Sequence[Anime]) -> tuple[list[str], dict[str, Anime]]:
    width = max(3, len(str(len(anime))))
    mapping: dict[str, Anime] = {}
    rows: list[str] = []
    for i, item in enumerate(anime, 1):
        row = f"{str(i).rjust(width)}  {item.title}"
        rows.append(row)
        mapping[row] = item
    return rows, mapping


def format_episode_rows(episodes: Sequence[Episode]) -> tuple[list[str], dict[str, Episode]]:
    rows: list[str] = []
    mapping: dict[str, Episode] = {}
    for ep in episodes:
        row = f"Episode {ep.number}"
        rows.append(row)
        mapping[row] = ep
    return rows, mapping


# ---------- app ----------

class App:
    def __init__(self, args: argparse.Namespace) -> None:
        set_json_output(bool(getattr(args, "json", False)))
        error = subtitle_preference_error(getattr(args, "sub_lang", None))
        if error:
            fail(error)
        self.args = args
        self.http = HttpClient()
        order = [x.strip().lower() for x in args.provider_order.split(",") if x.strip()]
        self.providers = ProviderManager(
            [
                HianimeProvider(self.http),
                AniLightProvider(self.http),
                AnimeAV1Provider(self.http),
                AnimeFlvProvider(self.http),
            ],
            order,
        )
        self.history = HistoryStore()
        self.session_store = DetachedSessionStore()
        self.menu = Menu(args.menu, args.menu_flags)
        listing_only = args.list_providers or getattr(args, "show_history", False)
        self.playback = None if listing_only else Playback(args)
        self.bundle_cache: dict[tuple[str, str, str, str], StreamBundle] = {}
        self.episode_cache: dict[tuple[str, str], list[Episode]] = {}
        self.fallback_map: dict[tuple[str, str], Anime] = {}
        self.last_stream: Optional[Stream] = None
        self.last_provider: Optional[str] = None
        self.last_subtitle: Optional[SubtitleTrack] = None
        self.subtitle_preference = getattr(args, "sub_lang", None) or "auto"
        self.subtitle_source: Optional[SubtitleSource] = None
        if (
            self.playback is not None
            and not getattr(args, "no_sub_search", False)
            # The Android relay only serves http(s); a cached file cannot ride it.
            and not self.playback._is_android()
        ):
            self.subtitle_source = AnimetoshoSubtitles(self.http)
        self._reference_cues: dict[str, list[float]] = {}

    def _search_anime(self, query: str) -> Anime:
        status(f"Searching for {sty(query, C.BOLD)}")
        try:
            results = self.providers.search(query, self.args.provider)
        except ProviderError as exc:
            fail(str(exc))
        if not results:
            fail("No results found.")
        rows, mapping = format_anime_rows(results)
        if self.args.select_nth:
            idx = self.args.select_nth - 1
            if not (0 <= idx < len(results)):
                fail("--select-nth is outside the result list.")
            chosen = results[idx]
        else:
            picked = self.menu.choose(rows, "Anime › ")
            if not picked:
                raise SystemExit(0)
            if picked[0] not in mapping:
                fail("Selection did not match any result.")
            chosen = mapping[picked[0]]
        provider = self.providers.get(chosen.provider)
        ok(f"Selected {chosen.title}  [{provider.display_name}]")
        return chosen

    def _from_history(self) -> tuple[Anime, str, bool]:
        entries = self.history.load()
        if not entries:
            fail("History is empty.")
        rows = [
            f"{e.title}  {sty('•', C.DIM)}  Episode {e.episode} "
            f"{'watched' if e.completed else 'unfinished'}  "
            f"{sty('• ' + e.provider, C.DIM)}"
            for e in entries
        ]
        mapping = dict(zip(rows, entries))
        picked = self.menu.choose(rows, "Continue › ")
        if not picked:
            raise SystemExit(0)
        if picked[0] in mapping:
            entry = mapping[picked[0]]
        else:
            # fzf --ansi strips ANSI codes from its output, so fall back
            # to an ANSI-insensitive match before giving up.
            stripped_rows = [_strip_ansi(r) for r in rows]
            needle = _strip_ansi(picked[0])
            if needle in stripped_rows:
                entry = entries[stripped_rows.index(needle)]
            else:
                fail("History selection did not match any entry.")
                raise SystemExit(1)
        return (
            Anime(entry.provider_id, entry.title, entry.provider),
            entry.episode,
            entry.completed,
        )

    @staticmethod
    def _match_score(left: str, right: str) -> float:
        a, b = _normalize_title(left), _normalize_title(right)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        return SequenceMatcher(None, a, b).ratio()

    def _choose_fallback_candidate(self, original: Anime, candidates: Sequence[Anime]) -> Optional[Anime]:
        if not candidates:
            return None
        normalized = _normalize_title(original.title)
        exact = [c for c in candidates if _normalize_title(c.title) == normalized]
        if len(exact) == 1:
            return exact[0]

        ranked = sorted(
            ((self._match_score(original.title, c.title), c) for c in candidates),
            key=lambda item: item[0],
            reverse=True,
        )
        if ranked:
            best_score = ranked[0][0]
            second = ranked[1][0] if len(ranked) > 1 else 0.0
            if best_score >= 0.94 and best_score - second >= 0.06:
                return ranked[0][1]

        # Ambiguous matches are shown to the user instead of silently guessing.
        shortlist = [c for score, c in ranked[:8] if score >= 0.50] or [c for _, c in ranked[:8]]
        if not shortlist:
            return None
        rows = [f"{c.title}  {sty('• ' + self.providers.get(c.provider).display_name, C.DIM)}" for c in shortlist]
        mapping = dict(zip(rows, shortlist))
        chosen = self.menu.choose(
            rows,
            "Fallback › ",
            compact=len(rows) <= 8,
            header=f"Primary source failed. Match {original.title} on a backup provider:",
        )
        if not chosen:
            return None
        picked_candidate = mapping.get(chosen[0])
        if picked_candidate is not None:
            return picked_candidate
        # fzf --ansi strips ANSI codes from its output.
        needle = _strip_ansi(chosen[0])
        for row, candidate in mapping.items():
            if _strip_ansi(row) == needle:
                return candidate
        return None

    def _find_fallback_anime(self, anime: Anime) -> Optional[Anime]:
        cached = self.fallback_map.get((anime.provider, anime.provider_id))
        if cached:
            return cached
        if self.args.provider != "auto":
            return None
        for name in self.providers.fallback_names(anime.provider):
            provider = self.providers.get(name)
            status(f"Trying backup provider {provider.display_name}")
            try:
                candidates = provider.search(anime.title)
            except ProviderError as exc:
                warn(f"{provider.display_name} search failed: {exc}")
                continue
            chosen = self._choose_fallback_candidate(anime, candidates)
            if chosen:
                self.fallback_map[(anime.provider, anime.provider_id)] = chosen
                ok(f"Matched backup: {chosen.title}  [{provider.display_name}]")
                return chosen
        return None

    def _episodes(self, anime: Anime) -> list[Episode]:
        key = (anime.provider, anime.provider_id)
        if key not in self.episode_cache:
            self.episode_cache[key] = self.providers.episodes(anime)
        return self.episode_cache[key]

    def _episodes_with_fallback(self, anime: Anime) -> tuple[Anime, list[Episode]]:
        try:
            return anime, self._episodes(anime)
        except ProviderError as exc:
            if self.args.provider != "auto":
                raise
            warn(f"{self.providers.get(anime.provider).display_name} episode list failed: {exc}")
            fallback = self._find_fallback_anime(anime)
            if not fallback:
                raise ProviderUnavailable(f"No backup provider could match {anime.title}.") from exc
            return fallback, self._episodes(fallback)

    def _pick_episodes(
        self, anime: Anime, continue_after: Optional[str], continue_completed: bool = True
    ) -> tuple[Anime, list[Episode], list[Episode]]:
        status("Loading episodes")
        try:
            anime, episodes = self._episodes_with_fallback(anime)
        except ProviderError as exc:
            fail(str(exc))
        if not episodes:
            fail("No episodes were found.")

        if continue_after is not None:
            idx = episode_index(episodes, continue_after)
            if idx is None:
                fail("No unwatched episode is available in history.")
            # An episode that was started but never finished is resumed, not
            # skipped: mpv restores its saved position. Only a completed one
            # advances. Legacy rows without a completion flag read as
            # completed, so they keep advancing exactly as they did before.
            if continue_completed:
                if idx + 1 >= len(episodes):
                    fail("No unwatched episode is available in history.")
                return anime, episodes, [episodes[idx + 1]]
            return anime, episodes, [episodes[idx]]

        if self.args.episode:
            selected = parse_episode_spec(self.args.episode, episodes)
            if not selected:
                fail(f"Invalid episode/range: {self.args.episode}")
            return anime, episodes, selected

        rows, mapping = format_episode_rows(episodes)
        picked = self.menu.choose(rows, "Episode › ", multi=True)
        selected = [mapping[row] for row in picked if row in mapping]
        if not selected:
            raise SystemExit(0)
        return anime, episodes, selected

    def _search_another_anime(self) -> Optional[tuple[Anime, list[Episode], Episode]]:
        """Search/select a new title without ending the current playback session."""
        clear_screen()
        banner("Search another anime")
        print()
        try:
            query = input(sty("  Search › ", C.BOLD, C.CYAN)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not query:
            return None

        status(f"Searching for {sty(query, C.BOLD)}")
        try:
            results = self.providers.search(query, self.args.provider)
        except ProviderError as exc:
            warn(str(exc))
            return None
        if not results:
            warn("No results found.")
            return None

        rows, mapping = format_anime_rows(results)
        picked = self.menu.choose(rows, "Anime › ")
        if not picked:
            return None
        chosen = mapping.get(picked[0])
        if chosen is None:
            needle = _strip_ansi(picked[0])
            chosen = next(
                (item for row, item in mapping.items() if _strip_ansi(row) == needle),
                None,
            )
        if chosen is None:
            warn("Selection did not match any result.")
            return None

        provider = self.providers.get(chosen.provider)
        ok(f"Selected {chosen.title}  [{provider.display_name}]")
        status("Loading episodes")
        try:
            chosen, episodes = self._episodes_with_fallback(chosen)
        except ProviderError as exc:
            warn(str(exc))
            return None
        if not episodes:
            warn("No episodes were found.")
            return None

        episode_rows, episode_mapping = format_episode_rows(episodes)
        episode_pick = self.menu.choose(episode_rows, "Episode › ")
        if not episode_pick:
            return None
        episode = episode_mapping.get(episode_pick[0])
        if episode is None:
            needle = _strip_ansi(episode_pick[0])
            episode = next(
                (item for row, item in episode_mapping.items() if _strip_ansi(row) == needle),
                None,
            )
        if episode is None:
            warn("Episode selection did not match any entry.")
            return None
        return chosen, episodes, episode

    def _clear_detached_session(self) -> None:
        store = getattr(self, "session_store", None)
        if store is not None:
            store.clear()

    def _save_detached_session(
        self,
        anime: Anime,
        current: Episode,
        quality: str,
    ) -> bool:
        playback = self.playback
        store = getattr(self, "session_store", None)
        if (
            playback is None
            or store is None
            or playback._is_android()
            or not playback._is_mpv()
            or playback.ipc_path is None
            or not playback.active()
        ):
            return False
        session = DetachedSession(
            socket=str(playback.ipc_path),
            player=playback.player,
            provider=anime.provider,
            provider_id=anime.provider_id,
            title=anime.title,
            episode=current.number,
            quality=self.last_stream.quality if self.last_stream else quality,
            mode=self.args.mode,
            source_provider=self.last_provider or anime.provider,
            subtitle_preference=self.subtitle_preference,
        )
        store.save(session)
        return True

    def _resume_detached_session(self, session: DetachedSession) -> int:
        assert self.playback is not None
        if not self.playback.adopt_detached_session(session):
            self._clear_detached_session()
            warn("The saved detached mpv session is no longer running.")
            return 1

        self.args.mode = session.mode
        self.subtitle_preference = session.subtitle_preference or "auto"
        anime = Anime(session.provider_id, session.title, session.provider)
        try:
            anime, episodes = self._episodes_with_fallback(anime)
        except ProviderError as exc:
            warn(f"Could not refresh episode metadata while reattaching: {exc}")
            episodes = [Episode(session.episode, session.episode)]

        idx = episode_index(episodes, session.episode)
        if idx is None:
            current = Episode(session.episode, session.episode)
            episodes = [current]
        else:
            current = episodes[idx]

        current_path = self.playback.current_path() or ""
        self.last_stream = Stream(session.quality or "auto", current_path)
        self.last_provider = session.source_provider or anime.provider
        self.last_subtitle = None
        ok(f"Reattached to {anime.title} Episode {current.number}.")
        self._interactive_loop(anime, episodes, current, session.quality or "best")
        return 0

    def _maybe_resume_detached_session(self, *, force: bool = False) -> Optional[int]:
        store = getattr(self, "session_store", None)
        if store is None or self.playback is None:
            return 1 if force else None
        session = store.load()
        if session is None:
            if force:
                warn("No detached ani-py mpv session was found.")
                return 1
            return None

        if not self.playback.adopt_detached_session(session):
            store.clear()
            if force:
                warn("The saved detached mpv session is no longer running.")
                return 1
            return None

        if force:
            return self._resume_detached_session(session)

        choice = self.menu.choose(
            ["Reattach controls", "Stop playback and search", "Exit"],
            "Detached › ",
            compact=True,
            header=f"Detached playback found: {session.title} • Episode {session.episode}",
        )
        if not choice or choice[0] == "Exit":
            self.playback.detach()
            return 0
        if choice[0] == "Reattach controls":
            return self._resume_detached_session(session)
        if choice[0] == "Stop playback and search":
            self.playback.stop()
            store.clear()
            self.playback = Playback(self.args)
            return None
        self.playback.detach()
        return 0

    def _resolve_on(self, anime: Anime, number: str) -> StreamBundle:
        episodes = self._episodes(anime)
        idx = episode_index(episodes, number)
        if idx is None:
            raise EpisodeNotFound(
                f"{self.providers.get(anime.provider).display_name} does not have episode {number}."
            )
        return self.providers.resolve(anime, episodes[idx], self.args.mode)

    def _bundle(self, anime: Anime, episode: Episode) -> StreamBundle:
        key = (anime.provider, anime.provider_id, episode.number, self.args.mode)
        if key in self.bundle_cache:
            return self.bundle_cache[key]

        status(f"Resolving Episode {episode.number} [{self.args.mode}] via {self.providers.get(anime.provider).display_name}")

        mapped = self.fallback_map.get((anime.provider, anime.provider_id))
        if mapped:
            try:
                bundle = self._resolve_on(mapped, episode.number)
                self.bundle_cache[key] = bundle
                return bundle
            except ProviderError as exc:
                warn(f"Cached backup {self.providers.get(mapped.provider).display_name} failed: {exc}")
                self.fallback_map.pop((anime.provider, anime.provider_id), None)

        try:
            bundle = self.providers.resolve(anime, episode, self.args.mode)
            self.bundle_cache[key] = bundle
            return bundle
        except ProviderError as primary_exc:
            if self.args.provider != "auto":
                fail(str(primary_exc))
            warn(f"{self.providers.get(anime.provider).display_name} stream failed: {primary_exc}")

        # Automatic failover is conservative: exact/high-confidence matches are
        # automatic; ambiguous title matches are presented to the user.
        fallback = self._find_fallback_anime(anime)
        if not fallback:
            fail(f"No backup source could resolve {anime.title} episode {episode.number}.")
        try:
            bundle = self._resolve_on(fallback, episode.number)
        except ProviderError as backup_exc:
            fail(f"Backup provider failed: {backup_exc}")
        self.bundle_cache[key] = bundle
        return bundle

    def _subtitle_reference(self, track: SubtitleTrack, referer: str) -> list[float]:
        """Cue starts of the provider's own track, fetched once per track."""
        cues = self._reference_cues.get(track.url)
        if cues is None:
            try:
                cues = subtitle_cue_starts(self.http.get(track.url, referer=referer))
            except (HttpError, ValueError) as exc:
                # ValueError: curl output is decoded as strict UTF-8.
                warn(f"Could not read the provider subtitle to verify external ones: {exc}")
                cues = []
            self._reference_cues[track.url] = cues
        return cues

    def _subtitle_lookup(self, bundle: StreamBundle, episode: Episode) -> Optional[SubtitleLookup]:
        """External subtitle search for this episode, or None when it must not run."""
        source = self.subtitle_source
        if source is None or not bundle.mal_id:
            return None
        if not self.providers.get(bundle.provider).capabilities.subtitles:
            # Burned-in subtitles: never stack a second one on top.
            return None
        reference_track = choose_subtitle_track(bundle, "auto")
        if reference_track is None:
            # Nothing of the provider's to verify a found file against.
            return None
        mal_id = bundle.mal_id

        def lookup(language: str) -> Optional[SubtitleTrack]:
            if not source.supports(language):
                return None
            reference = self._subtitle_reference(reference_track, bundle.referer)
            if not reference:
                return None
            status(f"Searching external subtitles ({language})")
            return source.find(mal_id, episode.number, language, reference)

        return lookup

    def _play_episode(
        self,
        anime: Anime,
        episode: Episode,
        quality: str,
        *,
        replace: bool = False,
        foreground: bool = False,
        keep_open: bool = True,
    ) -> int:
        # Guarded up front: the episode banner reads self.playback.player.
        assert self.playback is not None
        bundle = self._bundle(anime, episode)
        stream = choose_quality(bundle.streams, quality)
        subtitle_track = resolve_subtitle(
            bundle, self.subtitle_preference, self._subtitle_lookup(bundle, episode)
        )
        explicit = [
            entry for entry in parse_subtitle_preference(self.subtitle_preference)
            if entry.casefold() not in _SUBTITLE_OFF | _SUBTITLE_AUTO
        ]
        if bundle.subtitle_tracks() and subtitle_track is None and explicit:
            warn(
                f"Subtitle {self.subtitle_preference!r} is not available for this episode; "
                "continuing without an external subtitle."
            )
        self.last_stream = stream
        self.last_provider = bundle.provider
        self.last_subtitle = subtitle_track
        clear_screen()
        banner(f"{anime.title}  •  Episode {episode.number}  •  {stream.quality}")
        say()
        say(f"  {sty('Title', C.DIM)}    {anime.title}")
        say(f"  {sty('Episode', C.DIM)}  {episode.number}")
        say(f"  {sty('Mode', C.DIM)}     {self.args.mode.upper()}")
        say(f"  {sty('Quality', C.DIM)}  {stream.quality}")
        say(f"  {sty('Source', C.DIM)}   {self.providers.get(bundle.provider).display_name}")
        say(f"  {sty('Player', C.DIM)}   {Path(self.playback.player).name if self.playback.player != 'download' else 'download'}")
        subtitle_name = subtitle_track.name if subtitle_track else "off"
        if subtitle_track and subtitle_track.language:
            subtitle_name += f" [{subtitle_track.language}]"
        if subtitle_track and subtitle_track.source:
            subtitle_name += f" (external: {subtitle_track.source})"
        say(f"  {sty('Subtitle', C.DIM)} {subtitle_name}")
        say()
        play_kwargs = PlayKw(
            title=f"{anime.title} Episode {episode.number}",
            subtitle=subtitle_track.url if subtitle_track else None,
            referer=bundle.referer,
            mal_id=bundle.mal_id,
            episode=episode.number,
            subtitle_language=subtitle_track.language if subtitle_track else None,
            subtitle_label=subtitle_track.label if subtitle_track else None,
        )
        if replace:
            rc = self.playback.replace(stream, **play_kwargs)
        else:
            rc = self.playback.play(
                stream, foreground=foreground, keep_open=keep_open, **play_kwargs
            )
        if rc == 0:
            # Recorded as unfinished at launch: --continue resumes this episode
            # rather than skipping past whatever is left of it.
            self.history.update(anime, episode.number, completed=False)
            if (
                not foreground
                and self.playback is not None
                and not self.playback._is_android()
            ):
                # Foreground playback blocks until mpv exits, so the watcher is
                # only needed for the detached case.
                self._watch_completion(anime, episode)
        return rc

    def _stop_completion_watch(self) -> None:
        stop = getattr(self, "_completion_stop", None)
        if stop is not None:
            stop.set()

    def _watch_completion(self, anime: Anime, episode: Episode) -> None:
        """Record completion as soon as mpv reaches EOF.

        Closing the mpv window is an external event: there is no exit path to
        hook, and once the process is gone its end-of-file state cannot be
        queried. Recording at the moment it is observed means the last known
        state survives an abrupt close, instead of every window-closed episode
        looking unfinished.
        """
        self._stop_completion_watch()
        stop = threading.Event()
        self._completion_stop = stop
        playback = self.playback
        number = episode.number

        def watch() -> None:
            while not stop.is_set():
                try:
                    reached = playback is not None and playback.reached_eof()
                except Exception:  # noqa: BLE001 - a watcher must never crash
                    reached = False
                # Both None (player gone) and False mean "not proven finished",
                # so a plain truth test is correct here; the tri-state only
                # matters in _record_completion, which must not overwrite a
                # recorded finish with an unknown.
                if reached:
                    self.history.update(anime, number, completed=True)
                    return
                # Short enough that closing the window in the moment after an
                # episode ends still finds the recorded state.
                stop.wait(1.0)

        threading.Thread(target=watch, daemon=True).start()

    def _record_completion(self, anime: Anime, episode: Episode) -> None:
        """Persist whether the episode being left actually reached its end.

        Only mpv knows this. Without it, finishing an episode normally would
        leave history marked unfinished and `--continue` would replay it.
        """
        playback = self.playback
        if playback is None:
            return
        reached = playback.reached_eof()
        if reached is None:
            # The player is already gone, so this says nothing. Keep whatever
            # the completion watcher recorded while it was still reachable.
            return
        self.history.update(anime, episode.number, completed=reached)

    def _interactive_loop(self, anime: Anime, episodes: list[Episode], current: Episode, quality: str) -> None:
        if self.args.download or self.args.exit_after_play:
            return
        assert self.playback is not None
        while True:
            options = [
                "Next episode",
                "Previous episode",
                "Replay",
                "Choose episode",
                "Search another anime",
                "Change quality",
                "Change subtitle",
                "Detach & exit",
                "Stop & quit",
            ]
            state = "playing" if self.playback.active() else "player closed"
            actual_quality = self.last_stream.quality if self.last_stream else quality
            source = self.providers.get(self.last_provider).display_name if self.last_provider else anime.provider
            header = (
                f"{anime.title}\n"
                f"Episode {current.number}  •  {self.args.mode.upper()}  •  {actual_quality}  •  {source}  •  {state}"
            )
            picked = self.menu.choose(
                options,
                f"Episode {current.number} › ",
                compact=True,
                header=header,
            )
            if not picked:
                # Leaving the episode: record what mpv actually saw, because
                # this is the normal way ani-py is closed after a long watch.
                self._record_completion(anime, current)
                return
            action = picked[0]
            idx = episode_index(episodes, current.number)
            if idx is None:
                self._record_completion(anime, current)
                return
            if action == "Stop & quit":
                # Recorded before stopping: once mpv is gone its end-of-file
                # state is unknowable and every episode would look unfinished.
                self._record_completion(anime, current)
                self._clear_detached_session()
                self.playback.stop()
                return
            if action == "Detach & exit":
                self._record_completion(anime, current)
                saved = self._save_detached_session(anime, current, quality)
                self.playback.detach()
                if saved:
                    ok("Detached; run ani-py --attach to return to these controls.")
                else:
                    ok("Detached; playback continues in the background.")
                return
            if action == "Next episode":
                if idx + 1 >= len(episodes):
                    warn("Already at the last episode.")
                    continue
                current = episodes[idx + 1]
                self._play_episode(anime, current, quality, replace=True)
            elif action == "Previous episode":
                if idx == 0:
                    warn("Already at the first episode.")
                    continue
                current = episodes[idx - 1]
                self._play_episode(anime, current, quality, replace=True)
            elif action == "Replay":
                if not self.playback.replay():
                    self._play_episode(anime, current, quality, replace=True)
            elif action == "Choose episode":
                rows, mapping = format_episode_rows(episodes)
                chosen = self.menu.choose(rows, "Episode › ")
                if chosen:
                    picked_episode = mapping.get(chosen[0])
                    if picked_episode is None:
                        warn("Episode selection did not match any entry.")
                    else:
                        current = picked_episode
                        self._play_episode(anime, current, quality, replace=True)
            elif action == "Search another anime":
                target = self._search_another_anime()
                if target is None:
                    continue
                next_anime, next_episodes, next_episode = target
                try:
                    rc = self._play_episode(next_anime, next_episode, quality, replace=True)
                except SystemExit:
                    # Provider resolution errors should not tear down the
                    # current session while the existing player is still alive.
                    continue
                if rc == 0:
                    anime, episodes, current = next_anime, next_episodes, next_episode
            elif action == "Change quality":
                bundle = self._bundle(anime, current)
                qrows = list(dict.fromkeys(s.quality for s in bundle.streams))
                chosen = self.menu.choose(
                    qrows,
                    "Quality › ",
                    compact=True,
                    header=f"{anime.title} • Episode {current.number} • {self.providers.get(bundle.provider).display_name}",
                )
                if chosen:
                    quality = chosen[0]
                    self._play_episode(anime, current, quality, replace=True)
            elif action == "Change subtitle":
                bundle = self._bundle(anime, current)
                tracks = bundle.subtitle_tracks()
                if not tracks:
                    warn("This source does not expose switchable subtitle tracks.")
                    continue
                # The menu lists the provider's tracks only. While an external
                # subtitle plays, none of them is the current one.
                current_subtitle = (
                    self.last_subtitle
                    if self.last_subtitle is not None and self.last_subtitle.source
                    else resolve_subtitle(bundle, self.subtitle_preference)
                )
                rows, mapping = subtitle_menu_rows(tracks, current_subtitle)
                chosen = self.menu.choose(
                    rows,
                    "Subtitle › ",
                    compact=len(rows) <= 12,
                    header=f"{anime.title} • Episode {current.number} • {self.providers.get(bundle.provider).display_name}",
                )
                if not chosen:
                    continue
                selected = mapping.get(chosen[0])
                if chosen[0] not in mapping:
                    needle = _strip_ansi(chosen[0])
                    selected = next(
                        (track for row, track in mapping.items() if _strip_ansi(row) == needle),
                        None,
                    )
                if selected is None and _strip_ansi(chosen[0]) != "Off":
                    warn("Subtitle selection did not match any track.")
                    continue
                self.subtitle_preference = (
                    "off"
                    if selected is None
                    else f"label:{selected.label}"
                    if selected.label
                    else selected.language or "auto"
                )
                self.last_subtitle = selected
                if self.playback.set_subtitle(selected):
                    ok(f"Subtitle: {selected.name if selected else 'off'}")
                else:
                    self._play_episode(anime, current, quality, replace=True)

    @staticmethod
    def _auto_next_queue(
        episodes: Sequence[Episode], selected: Sequence[Episode], limit: int = 0
    ) -> list[Episode]:
        """Build the provider-independent playback queue.

        An explicit multi-episode selection/range is respected exactly and is
        never trimmed. A single selected episode means "start here and
        continue" through the already-loaded episode list, and that expansion is
        the only case `limit` caps, so one selection cannot silently become
        hundreds of episodes. `limit` of 0 means no cap. Trimming always
        announces what it dropped.
        """
        if len(selected) != 1:
            return list(selected)
        idx = episode_index(episodes, selected[0].number)
        queue = list(episodes[idx:]) if idx is not None else list(selected)
        if limit > 0 and len(queue) > limit:
            warn(
                f"Auto-next queue limited to {limit} episode(s) by --auto-next-limit; "
                f"{len(queue) - limit} later episode(s) will not be queued."
            )
            queue = queue[:limit]
        return queue

    def _run_auto_next(
        self,
        anime: Anime,
        episodes: list[Episode],
        selected: list[Episode],
        quality: str,
    ) -> int:
        assert self.playback is not None
        if not self.playback.auto_next_supported():
            fail(AUTO_NEXT_UNSUPPORTED)
        # Completion is detected from mpv's end-file event, which needs mpv to
        # end files rather than pause on them.
        self.playback.event_completion = True

        limit = max(0, getattr(self.args, "auto_next_limit", 0) or 0)
        queue = self._auto_next_queue(episodes, selected, limit)
        if not queue:
            return 0

        status(
            f"Auto-next: {len(queue)} episode{'s' if len(queue) != 1 else ''} "
            "(provider-independent, desktop mpv)"
        )
        now = NowPlaying()
        now.title(f"▶ {anime.title}")
        try:
            for index, episode in enumerate(queue):
                upcoming = queue[index + 1] if index + 1 < len(queue) else None
                now.title(
                    f"▶ {anime.title} · Ep {episode.number} ({index + 1}/{len(queue)})"
                )
                rc = self._play_episode(
                    anime,
                    episode,
                    quality,
                    replace=index > 0,
                    keep_open=True,
                )
                if rc != 0:
                    self.playback.stop()
                    return rc
                if index > 0 and not self.playback.resume():
                    warn("Auto-next loaded the next episode but could not resume mpv.")

                completion = self.playback.wait_for_completion(
                    on_wait=lambda e=episode, u=upcoming, i=index: self._draw_status(
                        now, anime, e, i + 1, len(queue), u
                    )
                )
                now.clear()
                if completion == "ipc-lost":
                    warn("Lost contact with mpv; stopping the auto-next queue.")
                elif completion == "stalled":
                    warn(
                        "Episode "
                        f"{episode.number}'s stream stopped responding; "
                        "stopping the auto-next queue."
                    )
                elif completion == "failed":
                    warn(
                        "mpv reported the stream ended in an error for "
                        f"Episode {episode.number}; stopping the auto-next queue."
                    )
                elif completion == "unsupported":
                    warn("mpv IPC became unavailable; stopping the auto-next queue.")
                if completion != "eof":
                    self.playback.stop()
                    return 0

                # Natural EOF is the only proof the episode was actually
                # finished, so this is the only place history is marked done.
                self.history.update(anime, episode.number, completed=True)

                if index + 1 >= len(queue):
                    self.playback.stop()
                    ok("Auto-next queue finished.")
                    return 0

                next_episode = queue[index + 1]
                ok(
                    f"Episode {episode.number} finished; "
                    f"starting Episode {next_episode.number}."
                )
        except (KeyboardInterrupt, SystemExit):
            self.playback.stop()
            raise
        return 0

    def _draw_status(
        self,
        now: NowPlaying,
        anime: Anime,
        episode: Episode,
        position: int,
        total: int,
        upcoming: Optional[Episode],
    ) -> None:
        """Refresh the in-place now-playing line for the current episode."""
        if not now.enabled:
            return
        # The episode's own number and its position in the queue are different
        # things whenever playback starts mid-season, so they are never shown
        # as one ratio: "Ep 19/8" would read as "19 out of 8".
        parts = [
            f"{sty('▶', C.CYAN)} {anime.title}",
            sty(f"Ep {episode.number} ({position}/{total})", C.DIM),
        ]
        playback = self.playback
        if playback is not None:
            marks = playback.progress()
            if marks is not None:
                # `position` above is the place in the queue; the playback
                # offset is a different quantity and must not reuse the name.
                offset, span = marks
                if span > 0:
                    parts.append(f"{clock(offset)}/{clock(span)} ({offset * 100 // span:.0f}%)")
                else:
                    parts.append(clock(offset))
        if upcoming is not None:
            parts.append(sty(f"→ Ep {upcoming.number}", C.DIM))
        line = "  ".join(parts)
        # ceiling: term_width is capped at the banner width; longer lines are
        # simply not truncated because a wrapped status line redraws badly.
        now.update(line)

    @staticmethod
    def _provider_json(provider: Provider) -> dict[str, object]:
        caps = provider.capabilities
        return {
            "name": provider.name,
            "display_name": provider.display_name,
            "capabilities": {"sub": caps.sub, "dub": caps.dub, "subtitles": caps.subtitles, "mal_id": caps.mal_id},
            "experimental": bool(provider.experimental),
        }

    def _event(self, name: str, **fields: object) -> None:
        """One headless event: a JSON line with --json, a status line otherwise."""
        if json_output():
            emit({"event": name, **fields})
            return
        detail = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        status(f"{name} {detail}".rstrip())

    def _run_headless(
        self,
        anime: Anime,
        episodes: list[Episode],
        selected: list[Episode],
        quality: str,
    ) -> int:
        """Play or download `selected` with no controller, reporting each step.

        The queue follows --auto-next's rules only when --auto-next is given;
        otherwise it is exactly the selection.
        """
        assert self.playback is not None
        if getattr(self.args, "auto_next", False):
            limit = max(0, getattr(self.args, "auto_next_limit", 0) or 0)
            queue = self._auto_next_queue(episodes, selected, limit)
        else:
            queue = list(selected)
        total = len(queue)
        rc = 0
        if self.args.download:
            for index, episode in enumerate(queue, 1):
                self._event("downloading", episode=episode.number, index=index, total=total)
                rc = self._play_episode(anime, episode, quality)
                if rc != 0:
                    self._event("download-failed", episode=episode.number, rc=rc)
                    break
                self._event("downloaded", episode=episode.number)
            self._event("ended", rc=rc)
            return rc

        # Completion comes from mpv's end-file event, which needs mpv to end
        # files rather than pause on them (same as --auto-next).
        self.playback.event_completion = True
        try:
            for index, episode in enumerate(queue, 1):
                self._event("resolving", episode=episode.number, index=index, total=total)
                rc = self._play_episode(anime, episode, quality, replace=index > 1, keep_open=True)
                if rc != 0:
                    self._event("error", message=f"Episode {episode.number} could not be played (exit code {rc}).")
                    break
                if index > 1 and not self.playback.resume():
                    warn("Loaded the next episode but could not resume mpv.")
                self._event(
                    "playing",
                    title=anime.title,
                    episode=episode.number,
                    provider=self.last_provider,
                    quality=self.last_stream.quality if self.last_stream else None,
                    subtitle=_subtitle_json(self.last_subtitle),
                    socket=str(self.playback.ipc_path) if self.playback.ipc_path else None,
                    index=index,
                    total=total,
                )
                completion = self.playback.wait_for_completion()
                if completion == "eof":
                    # Natural EOF is the only proof the episode was finished.
                    self.history.update(anime, episode.number, completed=True)
                    self._event("completed", episode=episode.number)
                    continue
                self._event("stopped", episode=episode.number, reason=completion)
                rc = 0 if completion == "closed" else 1
                break
        except (KeyboardInterrupt, SystemExit):
            self.playback.stop()
            raise
        self.playback.stop()
        self._event("ended", rc=rc)
        return rc

    def _selected_anime(self, spec: str, title: Optional[str]) -> Anime:
        provider_name, sep, provider_id = spec.partition(":")
        if not sep or not provider_name or not provider_id:
            fail("--select takes PROVIDER:ID, e.g. hianime:one-piece-100.")
        if provider_name not in self.providers.providers:
            fail(f"Unknown provider {provider_name!r} in --select.")
        if not title:
            title = next(
                (e.title for e in self.history.load() if e.provider == provider_name and e.provider_id == provider_id),
                provider_id,
            )
        return Anime(provider_id, title, provider_name)

    @staticmethod
    def _episode_state(episode: Episode, seen: Optional[HistoryEntry]) -> str:
        """The history keeps one row per anime, so earlier episodes count as watched."""
        if seen is None:
            return "new"
        if episode.number == seen.episode:
            return "watched" if seen.completed else "unfinished"
        try:
            return "watched" if float(episode.number) < float(seen.episode) else "new"
        except ValueError:
            return "new"

    def _print_episodes(self, anime: Anime) -> int:
        status("Loading episodes")
        try:
            anime, episodes = self._episodes_with_fallback(anime)
        except ProviderError as exc:
            fail(str(exc))
        seen = next(
            (e for e in self.history.load() if e.provider == anime.provider and e.provider_id == anime.provider_id),
            None,
        )
        rows = [
            {"number": ep.number, "id": ep.episode_id, "state": self._episode_state(ep, seen)}
            for ep in episodes
        ]
        if json_output():
            emit({
                "anime": {"provider": anime.provider, "id": anime.provider_id, "title": anime.title},
                "episodes": rows,
            })
            return 0
        print(f"{anime.title}  ({anime.provider})")
        for row in rows:
            print(f"{row['number']:>7}  {row['state']}")
        return 0

    def run(self) -> int:
        if self.args.clear_history:
            self.history.clear()
            ok("History cleared.")
            return 0

        if self.args.list_providers:
            names = list(dict.fromkeys(list(self.providers.order) + list(self.providers.providers)))
            if json_output():
                emit({"providers": [self._provider_json(self.providers.get(name)) for name in names]})
                return 0
            for name in names:
                p = self.providers.get(name)
                caps = []
                if p.capabilities.sub:
                    caps.append("sub")
                if p.capabilities.dub:
                    caps.append("dub")
                if p.capabilities.subtitles:
                    caps.append("subs")
                if p.capabilities.mal_id:
                    caps.append("MAL")
                tag = " [experimental]" if p.experimental else ""
                print(f"{p.name:10} {p.display_name:12} {', '.join(caps)}{tag}")
            return 0

        if getattr(self.args, "show_history", False):
            entries = self.history.load()
            if json_output():
                emit({"entries": [_history_json(entry) for entry in entries]})
                return 0
            for entry in entries:
                state = "watched" if entry.completed else "unfinished"
                print(f"{entry.episode:>7}  {state:10}  {entry.title}  ({entry.provider})")
            return 0

        if getattr(self.args, "auto_next", False):
            if self.args.download:
                fail("--auto-next cannot be combined with --download.")
            if getattr(self.args, "attach", False):
                fail("--auto-next cannot be combined with --attach.")
            if getattr(self.args, "no_detach", False):
                fail("--auto-next cannot be combined with --no-detach.")
            if self.args.exit_after_play:
                fail("--auto-next cannot be combined with --exit-after-play.")
            # Reject an unusable player here, before the search and episode
            # listing spend the user's time on a run that cannot advance.
            if self.playback is not None and not self.playback.auto_next_supported():
                fail(AUTO_NEXT_UNSUPPORTED)

        if getattr(self.args, "headless", False):
            conflict = headless_conflict(self.args)
            if conflict:
                fail(conflict)
            if (
                not self.args.download
                and self.playback is not None
                and not self.playback.auto_next_supported()
            ):
                fail(AUTO_NEXT_UNSUPPORTED)

        if getattr(self.args, "attach", False):
            result = self._maybe_resume_detached_session(force=True)
            return 1 if result is None else result

        if (
            not self.args.continue_watching
            and not self.args.download
            and not self.args.query
            and not getattr(self.args, "select", None)
            and not json_output()
        ):
            result = self._maybe_resume_detached_session(force=False)
            if result is not None:
                return result

        if self.args.continue_watching:
            if json_output():
                fail("--continue needs the interactive menu; use --history and --select instead.")
            anime, continue_after, continue_completed = self._from_history()
        elif getattr(self.args, "select", None):
            anime = self._selected_anime(self.args.select, getattr(self.args, "title", None))
            continue_after = None
            continue_completed = True
        else:
            query = " ".join(self.args.query).strip()
            if not query and json_output():
                fail("--json needs a query or --select.")
            if not query:
                clear_screen()
                banner("Search • select • watch")
                print()
                try:
                    query = input(sty("  Search › ", C.BOLD, C.CYAN)).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 130
            if not query:
                return 0
            if json_output():
                status(f"Searching for {query}")
                try:
                    results = self.providers.search(query, self.args.provider)
                except ProviderError as exc:
                    fail(str(exc))
                emit({"results": [
                    {"provider": a.provider, "id": a.provider_id, "title": a.title} for a in results
                ]})
                return 0
            anime = self._search_anime(query)
            continue_after = None
            continue_completed = True

        if getattr(self.args, "list_episodes", False):
            return self._print_episodes(anime)

        anime, episodes, selected = self._pick_episodes(anime, continue_after, continue_completed)

        if getattr(self.args, "headless", False):
            return self._run_headless(anime, episodes, selected, self.args.quality)

        if getattr(self.args, "auto_next", False):
            return self._run_auto_next(anime, episodes, selected, self.args.quality)

        rc = 0
        queued_playback = len(selected) > 1 and not self.args.download
        for ep in selected:
            rc = self._play_episode(
                anime,
                ep,
                self.args.quality,
                foreground=queued_playback,
                keep_open=not queued_playback,
            )
            if rc != 0:
                return rc

        if queued_playback or self.args.download or self.args.exit_after_play:
            return rc
        self._interactive_loop(anime, episodes, selected[-1], self.args.quality)
        return rc



# ---------- CLI ----------

_VERSION_RE = re.compile(r"^VERSION\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _declared_version(payload: bytes) -> Optional[str]:
    match = _VERSION_RE.search(payload.decode("utf-8", "replace"))
    return match.group(1) if match else None


def _version_key(version: str) -> Optional[tuple]:
    """Sortable key for a calendar version, or None if it is not numeric.

    Only the leading digits of each dot-separated component count, so a copy
    that still carries the legacy `0.5.2-rc11` semver parses as `(0, 5, 2)` and
    sorts below any CalVer date. That keeps `--update`'s downgrade guard working
    during the transition instead of disabling itself on every old install.
    A genuinely unparseable version returns None, and the guard then lets the
    content comparison decide rather than blocking an update on a bad string.
    """
    parts: list[int] = []
    for component in version.split("."):
        digits = re.match(r"\d+", component.strip())
        if not digits:
            return None
        parts.append(int(digits.group()))
    return tuple(parts) or None


def run_update(http: Optional[HttpClient] = None, target: Optional[Path] = None) -> int:
    """Replace this script with the latest checksummed release asset and exit."""
    path = target if target is not None else Path(__file__).resolve()
    if http is None:
        http = HttpClient()
    try:
        remote = http.get_bytes(UPDATE_URL, timeout=30)
        checksums = http.get(UPDATE_CHECKSUM_URL, timeout=30)
    except AniPyError as exc:
        print(f"{APP_NAME}: could not fetch the latest release: {exc}")
        return 1

    expected_digest: Optional[str] = None
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == "ani-py":
            candidate = parts[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", candidate):
                expected_digest = candidate
                break
    if expected_digest is None:
        print(f"{APP_NAME}: latest release is missing a valid ani-py checksum.")
        return 1

    digest = hashlib.sha256(remote).hexdigest()
    if not secrets.compare_digest(digest, expected_digest):
        print(f"{APP_NAME}: latest release checksum verification failed; leaving {path} untouched.")
        return 1

    local = path.read_bytes()
    if remote == local:
        print(f"{APP_NAME} {VERSION} is up to date with the latest release.")
        return 0

    # "Newer" is decided by VERSION rather than content so a local development
    # build ahead of the latest release is never silently rolled back.
    remote_version = _declared_version(remote)
    local_version = _declared_version(local)
    remote_key = _version_key(remote_version) if remote_version else None
    local_key = _version_key(local_version) if local_version else None
    if remote_key and local_key and remote_key < local_key:
        print(
            f"{APP_NAME}: latest release is older than this copy "
            f"({remote_version} < {local_version}); leaving {path} untouched. "
            "Re-run install.sh to force the latest published release if that is what you want."
        )
        return 1

    # A 200 HTML error page must never overwrite a working install.
    if not remote.startswith(b"#!"):
        print(
            f"{APP_NAME}: the copy fetched from the latest release does not look like a script; "
            "leaving the installed copy untouched."
        )
        return 1

    handle, staged = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".new")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(remote)
        os.chmod(staged, 0o755)
        os.replace(staged, path)
    except PermissionError:
        print(
            f"{APP_NAME}: no permission to write {path}. Try: sudo {APP_NAME} --update"
        )
        return 1
    except OSError as exc:
        print(f"{APP_NAME}: could not write {path}: {exc}")
        return 1
    finally:
        if os.path.exists(staged):
            os.unlink(staged)

    print(
        f"updated {path} from the latest release "
        f"({len(local)} -> {len(remote)} bytes, sha {digest[:7]})"
    )
    print("re-run the installer with --deps if you are missing external tools")
    return 0

def env_int(name: str, default: int) -> int:
    """Read a non-negative integer environment variable, tolerating junk."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        warn(f"Ignoring invalid {name}={raw.strip()!r}; using {default}.")
        return default
    return max(0, value)


def build_parser() -> argparse.ArgumentParser:
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        formatter_class=formatter,
        description="A polished, standalone Python anime CLI (stdlib only).",
        epilog=textwrap.dedent(
            """
            examples:
              ani-py "frieren"
              ani-py -q 1080 -e 3 "dandadan"
              ani-py --dub -e 1-4 "one piece"
              ani-py -c
              ani-py -d -e 1-12 "pluto"
              ani-py --attach
              ani-py --update

            environment:
              ANI_PY_PLAYER          preferred player (mpv, vlc, iina, auto, or executable)
              ANI_PY_PLAYER_FLAGS    extra player flags
              ANI_PY_IPC_SOCKET      override private mpv IPC socket (advanced)
              ANI_PY_MENU            fzf, rofi, dmenu, or fallback terminal UI
              ANI_PY_MENU_FLAGS      extra menu flags
              ANI_PY_DOWNLOAD_DIR    download destination
              ANI_PY_SUB_LANG        preferred subtitle language/label or priority list, auto, or off
              ANI_PY_SUB_SEARCH      0 to never search external sources for a missing subtitle language
              ANI_PY_AUTO_NEXT       1 to auto-play following episodes (desktop mpv IPC)
              ANI_PY_AUTO_NEXT_LIMIT cap on --auto-next's auto-expanded queue (0 = no limit)
              ANI_PY_HIST_DIR        state directory root
              ANI_PY_CURL            curl/curl-impersonate executable
              ANI_PY_PROVIDER        auto, hianime, anilight, animeav1, or animeflv
              ANI_PY_PROVIDER_ORDER  failover order (default hianime; backups are opt-in)
              ANI_PY_ANILIGHT_URL     override AniLight site base URL
              ANI_PY_ANILIGHT_API_URL override AniLight API base URL
              ANI_PY_ANIMEAV1_URL     override AnimeAV1 site base URL
              ANI_PY_ANIMEFLV_URL     override AnimeFLV site base URL
              NO_COLOR               disable ANSI color
            """
        ),
    )
    parser.add_argument("query", nargs="*", help="anime search query")
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_watching",
        action="store_true",
        help="resume the last episode from history; if it was finished, start the next one",
    )
    parser.add_argument("--attach", action="store_true", help="reattach controls to the last detached desktop mpv session")
    parser.add_argument("-d", "--download", action="store_true", help="download instead of play")
    parser.add_argument("-D", "--delete-history", dest="clear_history", action="store_true", help="clear watch history")
    parser.add_argument("-e", "--episode", "-r", "--range", dest="episode", help="episode or range, e.g. 4 or 4-9")
    parser.add_argument("-q", "--quality", default=os.getenv("ANI_PY_QUALITY", "best"), help="best, worst, 360, 480, 720, 1080")
    parser.add_argument("--sub-lang", default=os.getenv("ANI_PY_SUB_LANG", "auto"), help="subtitle language/label for playback/downloads, a comma-separated priority list (latino,es,en), or auto/off")
    parser.add_argument(
        "--no-sub-search",
        action="store_true",
        default=os.getenv("ANI_PY_SUB_SEARCH", "1") == "0",
        help="never look for a missing --sub-lang language in external subtitle sources",
    )
    parser.add_argument("-S", "--select-nth", type=int, help="select search result by index")
    parser.add_argument(
        "--provider",
        choices=["auto", "hianime", "anilight", "animeav1", "animeflv"],
        default=os.getenv("ANI_PY_PROVIDER", "auto"),
        help="source provider (default: auto with failover)",
    )
    parser.add_argument(
        "--provider-order",
        default=os.getenv("ANI_PY_PROVIDER_ORDER", "hianime"),
        help="comma-separated auto-failover order (default: hianime; AniLight is opt-in)",
    )
    parser.add_argument("--list-providers", action="store_true", help="show configured providers and exit")
    parser.add_argument("--json", action="store_true", help="machine-readable output: JSON lines on stdout, messages on stderr, no menus")
    parser.add_argument("--history", dest="show_history", action="store_true", help="print the watch history (most recent first) and exit")
    parser.add_argument("--select", metavar="PROVIDER:ID", help="pick an anime by provider and id without searching, e.g. hianime:one-piece-100")
    parser.add_argument("--title", help="display name for --select (default: the history title, else the id)")
    parser.add_argument("--list-episodes", action="store_true", help="print the episodes of the selected anime and exit")
    parser.add_argument("--headless", action="store_true", help="play or download without the interactive controller: wait for the player and report events (desktop mpv)")
    parser.add_argument("--dub", dest="mode", action="store_const", const="dub", help="use dubbed stream")
    parser.add_argument("--sub", dest="mode", action="store_const", const="sub", help="use subtitled stream")
    parser.set_defaults(mode=os.getenv("ANI_PY_MODE", "sub"))
    parser.add_argument(
        "-p",
        "--player",
        default=os.getenv("ANI_PY_PLAYER"),
        help="player: mpv, vlc, iina, auto, or a custom executable (Android: auto, vlc, mpv)",
    )
    parser.add_argument("--player-flag", action="append", default=[], help="extra player argument (repeatable; use --player-flag='--flag' for dash-flags)")
    parser.add_argument("--ipc-socket", help="mpv IPC socket path (default: private per ani-py process)")
    parser.add_argument("--menu", choices=["fzf", "rofi", "dmenu"], help="interactive menu frontend")
    parser.add_argument("--menu-flags", default="", help="extra menu frontend flags (use --menu-flags='--flag' for dash-flags)")
    parser.add_argument("--skip", action="store_true", default=os.getenv("ANI_PY_SKIP_INTRO", "0") == "1", help="use ani-skip with mpv")
    parser.add_argument(
        "--auto-next",
        action="store_true",
        default=os.getenv("ANI_PY_AUTO_NEXT", "0") == "1",
        help="auto-play following episodes after natural EOF (desktop mpv IPC only)",
    )
    parser.add_argument(
        "--auto-next-limit",
        type=int,
        default=env_int("ANI_PY_AUTO_NEXT_LIMIT", AUTO_NEXT_DEFAULT_LIMIT),
        help=(
            "cap on episodes auto-expanded from a single selection, "
            "0 for no limit (default: %(default)s); an explicit selection or "
            "range is never trimmed"
        ),
    )
    parser.add_argument("--no-detach", action="store_true", default=os.getenv("ANI_PY_NO_DETACH", "0") == "1", help="keep player attached")
    parser.add_argument("--exit-after-play", action="store_true", default=os.getenv("ANI_PY_EXIT_AFTER_PLAY", "0") == "1", help="exit after player closes/launches")
    parser.add_argument(
        "--android-debug",
        action="store_true",
        default=os.getenv("ANI_PY_ANDROID_DEBUG", "0") == "1",
        help="print Android playback/relay diagnostics to stderr (harmless off Android)",
    )
    parser.add_argument(
        "-U",
        "--update",
        action="store_true",
        help="replace this script with the latest verified release and exit",
    )
    parser.add_argument("--_android-relay-config", help=argparse.SUPPRESS)
    parser.add_argument("-V", "--version", action="version", version=f"{APP_NAME} {VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.update:
        return run_update()
    if args._android_relay_config:
        return run_android_relay(args._android_relay_config)
    try:
        return App(args).run()
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
