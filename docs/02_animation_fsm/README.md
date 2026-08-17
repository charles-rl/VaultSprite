# Module 2 — Sprite Animation FSM Engine

## 1. Module Overview & Objective

Implements the probabilistic finite-state machine that picks which sprite animation to play next. A `config.json` schema declares states (`idle`, `walking`, `sleeping`, `talking`, …), each with sprite GIF, frame dims, optional per-frame movement vector `[dx, dy]`, and a weighted `transitions_to` table. When a state's frames finish, the next state is drawn from those weights.

Maps to **Module 2** of `IMPLEMENTATION_OUTLINE.md`; produces `animation_fsm.py` with a `get_next_state()` method returning the next sprite asset, duration, and position offsets.

Extraction source: **`Shirros/desktop-pet`** (Tkinter app; we port the *logic* into PySide6). Key files: `util.py` (weighted random), `pet.py` (state machine), `main.py` (drive loop), `assets/cave_chaos/config.json` (schema).

## 2. Minimum Required Libraries

| Package | Why |
|---|---|
| `PySide6` | Renders the sprite in the overlay `QLabel` and drives the animation tick (replaces Tkinter) |
| `json` / `random` (stdlib) | Config parsing + weighted selection |

No system drivers. Reference repo uses Tkinter + Pillow-free GIF decoding (`tk.PhotoImage`); PySide6's `QMovie` handles GIF frames natively, so no Pillow dependency is required for GIFs.

## 3. Source Code Extraction (Verbatim)

### 3.1 Weighted random selection — `util.py`

```python
def normalize(list):
    mag = sum(list)
    return [v / mag for v in list]

def make_cum(list):
    acc = 0
    for i in range(len(list)):
        temp = list[i]
        list[i] = acc
        acc += temp
    return list

class WeightedRandomMap:
    def __init__(self, list):
        self.names = [obj["name"] for obj in list]
        self.P = make_cum(normalize([obj["probability"] for obj in list]))
        assert len(self.names) == len(self.P)
    def get_rand(self):
        val = random.random()
        for i, p in enumerate(self.P):
            if p > val:
                return self.names[i - 1]
        return self.names[-1]
```

### 3.2 State + pet machine — `pet.py`

```python
from util import WeightedRandomMap
import tkinter as tk
from os.path import join

def read_frames(impath):
        output = []
        i = 0
        while True:
            try:
                new_frame = tk.PhotoImage(file=join(impath),format=f'gif -index {i}')
                output.append(new_frame)
            except:
                break
            i += 1
        return output

class PetState:
    def __init__(self, json_obj, impath):
        self.name = json_obj['state_name']
        self.frames = read_frames(join(impath, json_obj['file_name']))
        self.ox, self.oy, self.w, self.h = json_obj['dims']
        if 'move' in json_obj:
            self.dx, self.dy = json_obj['move']
        else:
            self.dx, self.dy = 0, 0
        self.next_states = WeightedRandomMap(json_obj['transitions_to'])


class Pet:
    def __init__(self, states, window):
        self.states = states
        self.window = window
        self.current_state = list(states.values())[0]
        self.__current_frame = 0
        self.x, self.y = 45, 800

    def next_frame(self):
        output = self.current_state.frames[self.__current_frame]
        self.__current_frame += 1
        if self.__current_frame == len(self.current_state.frames):
            self.__state_change()
        self.x, self.y = (
            self.x + self.current_state.dx), (self.y + self.current_state.dy)
        return output

    def __state_change(self):
        self.set_state(self.current_state.next_states.get_rand())

    def set_state(self, name: str):
        self.current_state = self.states[name]
        self.__current_frame = 0
```

### 3.3 Drive loop — `main.py`

```python
import tkinter as tk
import json
from pet import Pet, PetState
from os.path import join
import sys

if len(sys.argv) >= 2:
    CONFIG_PATH = sys.argv[1]
else:
    CONFIG_PATH = "assets\\bonzi\\"

def create_event_func(event, pet):
    if event["type"] == "state_change":
        return lambda e: pet.set_state(event["new_state"])
    elif event["type"] == "chatgpt":
        return lambda e: pet.start_chat(event["prompt"], event["listen_state"], event["response_state"], event["end_state"])


def update():
    frame = pet.next_frame()
    label.configure(image=frame)
    window.geometry(
        f'{pet.current_state.w}x{pet.current_state.h}+{pet.x + pet.current_state.ox}+{pet.y + pet.current_state.oy}')
    window.after(100, update)


if __name__ == "__main__":
    window = tk.Tk()
    with open(join(CONFIG_PATH, "config.json")) as config:
        config_obj = json.load(config)
        states = {state['state_name']: PetState(state, CONFIG_PATH) for state in config_obj["states"]}
        # Validate
        for state in states.values():
            for state in state.next_states.names:
                assert state in states
        pet = Pet(states, window)
        for event in config_obj["events"]:
            event_func = create_event_func(event, pet)
            if event["trigger"] == "click":
                window.bind("<Button-1>", event_func)
    # window configuration
    window.config(highlightbackground='black')
    label = tk.Label(window, bd=0, bg='black')
    window.overrideredirect(True)
    window.wm_attributes('-transparentcolor', 'black')
    label.pack()
    window.after(1, update)
    window.mainloop()
```

### 3.4 Config schema — `assets/cave_chaos/config.json` (representative states)

```json
{
    "states": [
        {
            "state_name": "miner_idle",
            "dims": [0, 0, 45, 50],
            "file_name": "miner_idle.gif",
            "transitions_to": [
                { "name": "miner_idle", "probability": 0.4 },
                { "name": "miner_walk", "probability": 0.4 },
                { "name": "miner_irish", "probability": 0.2 }
            ]
        },
        {
            "state_name": "miner_walk",
            "dims": [0, 0, 45, 50],
            "move": [2, 0],
            "file_name": "miner_walk.gif",
            "transitions_to": [
                { "name": "miner_walk", "probability": 0.7 },
                { "name": "miner_run", "probability": 0.2 },
                { "name": "miner_tired", "probability": 0.1 }
            ]
        },
        {
            "state_name": "miner_run",
            "dims": [0, 0, 45, 50],
            "move": [4, 0],
            "file_name": "miner_run.gif",
            "transitions_to": [
                { "name": "miner_walk", "probability": 0.3 },
                { "name": "miner_run", "probability": 0.7 }
            ]
        },
        {
            "state_name": "miner_sleeping",
            "dims": [0, 0, 50, 50],
            "file_name": "miner_sleeping.gif",
            "transitions_to": [
                { "name": "miner_sleeping", "probability": 0.9 },
                { "name": "miner_wakeup", "probability": 0.1 }
            ]
        }
    ]
}
```

## 4. Logic & Data Flow Breakdown

1. **Schema → object graph** (`main.py:30–38`): `config.json` is loaded; each `states[]` entry is wrapped in `PetState`, indexed by `state_name`. A validation pass asserts every transition target exists (fail-fast on typo'd state names). `dims = [ox, oy, width, height]` gives the sprite's frame size plus an anchor offset relative to the pet's logical `(x, y)` cursor.
2. **Movement vectors** (`PetState.__init__:22–26`): optional `move: [dx, dy]` becomes the per-frame translation applied by `next_frame()` (e.g. `miner_walk` drifts +2 px/frame; `miner_run` +4 px/frame). No `move` key ⇒ `(0, 0)`. Offsets let a `flip` animation with `dims: [-20, -20, 70, 70]` temporarily overhang the anchor box.
3. **Weighted transitions** (`util.py`):
   - `normalize` divides every weight by the sum — weights need **not** sum to 1 (see `miner_irish`: 0.1+0.1+0.1 = 0.3).
   - `make_cum` converts weights into a cumulative distribution in-place. Note the **off-by-one trap**: it writes the running sum into index `i` *before* adding the current value, so `P[i]` holds the cumulative total of all previous entries, and the first entry is forced to 0.
   - `get_rand` rolls `random.random()` and walks the CDF; because `P[i]` is "sum of entries before i", a roll in `(P[i-1], P[i])` correctly lands on name `i-1`. The final `return self.names[-1]` catches rolls that exceed all but the last bucket. The `names[i-1]` lookup is the fragile negative-index trick — preserve it when porting.
4. **Frame advance** (`Pet.next_frame`): increments a frame counter; when it reaches the frame list length, the state-change hook fires and a *new* state is chosen from `current_state.next_states.get_rand()` before the move vector is applied. So the movement from the **new** state starts immediately on the transition frame.
5. **Drive loop** (`main.py:19–24`): a fixed **100 ms** `window.after` tick advances the frame, paints the `PhotoImage` onto the label, and repositions the window via `geometry(wxh+x+ox, y+oy)`. There are **no per-state durations** — every state runs at 100 ms/frame regardless of its GIF's native speed. This is the single most important refactoring target (M5/outline wants explicit durations).
6. **Rendering model**: Tkinter with color-key transparency (`-transparentcolor black`). For PySide6, replace with `QMovie` (GIF) or `QLabel.setPixmap` per frame — the FSM logic itself is transport-agnostic.

## 5. Refactoring & Integration Notes

Target: `animation_fsm.py` with a **`get_next_state()`** method. Keep it pure (no Qt imports in the state machine — Qt lives in the overlay module) so it is unit-testable headlessly.

Step-by-step:

1. **Port `WeightedRandomMap` verbatim** into `animation_fsm.py` (or a small `_weighted.py`). Add `__call__` = `get_rand()` and an `expect(value)` helper used by deterministic tests (`random.seed` + replay).
2. **Model**:
   - `class SpriteState`: holds `name`, `sprite_path` (resolved relative to a `base_dir`), `dims`/offset `(ox, oy)`, `dx/dy`, and the `WeightedRandomMap`.
   - `class AnimationFSM`: `load(config_dir)` builds the `{name: SpriteState}` map and runs the transition-target validation from `main.py:34–36`.
3. **Implement the contract**:
   - `get_next_state(current: str) -> (next_name, asset_path, duration_ms, offset_x, offset_y)`.
   - `duration_ms`: since the reference has no durations, derive per-state from the config or add a `"duration_ms"` key to our schema (default 100 ms to match the reference). The outline explicitly wants durations returned.
   - Movement: advance the internal `(x, y)` by `dx/dy` on each transition and fold it into the returned offsets (or emit `position_delta = Signal(float, float)` for terrain physics).
4. **Config schema extension** (backwards-compatible): add optional `"duration_ms"` and keep `transitions_to`, `move`, `dims` as-is. Add a `talking` state per the outline (it is absent in `cave_chaos`; `bonzi/config.json` exercises chat via `events[]` of type `chatgpt`).
5. **Drive loop**: replace `window.after(100, ...)` with a `QTimer` in the overlay: on each tick call `get_next_state()` only when the current state's frames are exhausted; apply the returned pixmap via `sprite_changed` signal. Play each GIF with `QMovie` (handles internal frame timing) or a frame-slicer using `QLabel`.
6. **External nudges**: expose `force_state(name)` (used by M8 health nudges to force `stretch_nudge`, and by click events) and `state_changed = Signal(str)` so stat/health modules can react.
7. **Cleanups**: strip the OpenAI/`openai_query`, `pyttsx3`/`speak`, and `start_chat` glue — that belongs to the remote agent (M6), not the FSM. Drop Tkinter imports entirely.
8. **Testing**: with the FSM kept Qt-free, `pytest` can load `config.json`, assert all transition targets exist, and verify weighted selection converges to the declared distribution (chi-square or seeded expectation) — no GUI needed.

## 6. Source Files (Reference Copies)

Full verbatim copies from `Shirros/desktop-pet` (+ one PySide6 GIF-rendering reference from koishi), kept locally:

| File | Purpose |
|---|---|
| `source/util.py` | `normalize`/`make_cum`/`WeightedRandomMap` — the weighted-random core, verbatim |
| `source/pet.py` | `PetState` + `Pet` — frame advance, state change hook, move-vector application |
| `source/main.py` | Drive loop, config load + transition-target validation, Tkinter window setup |
| `source/config_cave_chaos.json` | Richest schema example: `move`, transitions, event click handlers |
| `source/config_bonzi.json` | Second example (no `move` key, chat/`chatgpt` events) |
| `source/koishi_pet_animations.py` | `PetAnimator` from koishi — PySide6 `QMovie`-based GIF playback (the Tkinter→PySide6 rendering port) |
