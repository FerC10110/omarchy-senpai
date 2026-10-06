# Senpai

Anime from the Omarchy bar: search, continue, download. [ani-py](https://github.com/ferc10110/ani-py)
with Latin American subtitles, played in mpv.

![Senpai finder](preview.png)

A bar widget shows what is playing; a finder overlay searches HiAnime,
AnimeAV1 and AnimeFLV, lists episodes with what you have already watched,
plays the one you pick in mpv (with `latino`/`es`/`en` subtitles looked up
when the provider has none) and downloads single episodes or ranges in the
background. Playback keeps running after the shell restarts.

## Requirements

- Omarchy 4.0.4 or newer (the Quickshell-based shell)
- `python3`, `mpv`, `notify-send` (already on Omarchy)
- `yt-dlp` for downloads (`sudo pacman -S yt-dlp`)

## Install

```sh
omarchy plugin add https://github.com/ferc10110/omarchy-senpai --enable
```

From a local checkout instead:

```sh
omarchy plugin add ~/path/to/omarchy-senpai --enable
```

Then add a keybinding (`contrib/bindings.lua`, copy into
`~/.config/hypr/bindings.lua`):

```lua
o.bind("SUPER + SHIFT + A", "Senpai", "omarchy-shell shell toggle io.github.ferc10110.senpai")
```

and, if you want it in the Omarchy menu, merge `contrib/omarchy-menu.jsonc`
into `~/.config/omarchy/extensions/omarchy-menu.jsonc` (a *Senpai* submenu
with *Find anime*, *Continue watching* and *Stop playback*).

## Usage

Open the finder with the keybinding or by clicking the bar glyph. It starts
on **Continue** (the next episode of the last anime you watched) and your
recent anime; typing searches.

| Key | Does |
| --- | --- |
| type | search (home/results); jump to an episode number (episodes) |
| `Enter` | continue / open episodes / play the highlighted episode |
| `Esc` | back; closes the finder from home |
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | move |
| `Ctrl+D` | download the highlighted episode |
| `Ctrl+Shift+D` | download a range: type `1-12` or `3,5` and press `Enter` |
| `Alt+P` / `Alt+A` / `Alt+Q` / `Alt+N` | cycle provider / audio (sub, dub) / quality / auto-next |
| `Ctrl+Space` / `Ctrl+N` / `Ctrl+S` | pause, next episode, stop |

Bar widget: left click opens the finder, right click pauses/resumes, middle
click stops. While something plays the label shows `Title · episode`; a
`↓2` suffix counts running downloads. Hover for the position, duration and
subtitle in use.

Downloads go to `~/Videos/anime` when that folder exists, else `~/Downloads`
(or the folder in the *Download folder* setting). A notification arrives when
each one finishes or fails.

## Settings

Right-click the bar → *Edit widgets* (or edit the entry in
`~/.config/omarchy/shell.json`).

| Key | Default | Meaning |
| --- | --- | --- |
| `subLang` | `latino,es,en` | subtitle languages, best first; `latino` never falls back to European Spanish |
| `provider` | `auto` | `auto`, `hianime`, `animeav1` or `animeflv` |
| `dub` | `false` | prefer dubbed audio when the provider has it |
| `quality` | `best` | `best`, `1080`, `720`, `480`, `360`, `worst` |
| `autoNext` | `false` | keep playing the following episodes |
| `subSearch` | `true` | look for missing subtitles in external sources (Animetosho) |
| `skipIntro` | `false` | skip openings with ani-skip |
| `downloadDir` | `` | download folder (empty: `~/Videos/anime` or `~/Downloads`) |
| `showTitleInBar` | `true` | show the title in the bar while playing |
| `barLabelMaxWidth` | `180` | max width of the bar label, px |
| `aniPyPath` | `` | use another `ani_py.py` instead of the bundled copy |

The `Alt+…` keys in the finder change the same settings and persist them.

## How it works

The shell never runs `ani_py.py` itself. `bin/senpai` is the one door: it
runs `ani_py.py --json` for searches, episode lists and the history, and
starts playbacks and downloads under a small detached supervisor so they
survive a shell reload. State lives in `$XDG_RUNTIME_DIR/senpai/`:

- `now-playing.json` — what plays (pid, provider, id, title, episode, socket)
- `events.jsonl` — ani-py's headless events of the current playback
- `mpv.sock` — mpv's IPC socket; the widget reads pause/position through it
- `downloads/<id>.json` — one record per download (`running`, `done`, `failed`)
- `ani-py.log`, `downloads/<id>.log` — ani-py's stderr, for troubleshooting

The helper can be used from scripts too:

```sh
H=~/.config/omarchy/plugins/io.github.ferc10110.senpai/bin/senpai
python3 $H search "one piece"
python3 $H episodes hianime one-piece-1 --title "One Piece"
python3 $H play hianime one-piece-1 -e 4 --title "One Piece" --sub-lang latino,es,en
python3 $H download hianime one-piece-1 -e 1-12 --title "One Piece"
python3 $H continue
python3 $H status
python3 $H stop
```

## Updating the bundled ani-py

`bin/ani_py.py` is a copy of ani-py with the machine-readable mode this plugin
needs. `scripts/sync-ani-py.sh [path/to/ani_py.py]` refreshes it (it refuses
downgrades and copies with no `--headless` support).

## Development

- `scripts/test.sh` — helper tests (Python, against a fake ani-py), model
  tests (`node --test`), manifest checks, `omarchy-plugin-validate`
- `scripts/check-live.sh [preview.png]` — drives the installed plugin through
  `omarchy-shell shell call` and takes a screenshot
- Omarchy hot-reloads a plugin when its folder changes, but the QML component
  cache keeps the old files: after editing a `.qml` file run
  `omarchy-restart-shell` to load it.

## Uninstall

```sh
omarchy plugin disable io.github.ferc10110.senpai
omarchy plugin remove io.github.ferc10110.senpai
rm -rf "$XDG_RUNTIME_DIR/senpai"
```

Your watch history stays in `~/.local/state/ani-py/`.

## License

MIT. `bin/ani_py.py` is MIT-licensed ani-py; see `THIRD_PARTY_NOTICES.md`.
