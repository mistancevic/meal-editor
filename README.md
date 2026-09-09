# Meal Editor

Build a day of meals the way you cut a video.

Video and audio editors all work the same way: you have tracks, you drop
pieces of material onto them, you add effects and transitions, and at the
end you render the result. A day of eating fits that shape exactly — so
this app is a timeline editor for meals.

| Editor idea | Here |
|---|---|
| **Library** (media bin) | ~70 ingredients with per-100 g macros (`data/ingredients.json`), tagged for restrictions (lactose, fish, gluten, nuts, soy, alcohol…) |
| **Tracks** | one lane per part of the plate: protein · carbs · vegetables · fats & sauces · extras & drinks — each with mute and solo |
| **Clips** | an ingredient portion placed at a time of day; drag to move, trim the edges, snap to 5 minutes and to neighbours, drag across lanes |
| **Effects** | how the clip is prepared (`data/prep_effects.json`): grilled −10 % fat, pan-fried +6 g oil per 100 g, breaded, cheese topping, honey glaze… stacked on a clip, macros follow |
| **Transitions** | what happens *between* meals: training, a walk, a fasting window, sleep — they shape the timing notes |
| **Monitor** | live calories and protein against the daily targets with the tolerance band drawn in; carbs and fat alongside |
| **Render** | the finished day: each meal with the macro math shown, the ±150 kcal / ±10 g protein check, restriction and dislike warnings on the offending clips, timing notes, and a plain-text plan you can copy or download |

Clips that sit close together become one meal and are labelled from the
clock (breakfast, lunch, snack, dinner). A **client profile** carries the
daily targets, ingredient tags to avoid, dislikes, usual bedtime and whether
tomorrow is a training day; it travels with the project.

## Run it

```bash
python app.py            # Python 3.10+, standard library only — nothing to install
# open http://localhost:8000
```

Two demo days are built in (a muscle-gain client and a fat-loss client with
a lactose restriction). Projects autosave in the browser and can be saved
and loaded as JSON.

On a phone the layout changes: the monitor stays at the top, the timeline
comes first, the library opens as a drawer from the **+ Library** button, and
tapping a clip slides up the inspector. Add ingredients with **+** (they land
at the playhead); drag-and-drop is a desktop thing.

Keyboard: `drag` move · edges trim · `Del` delete · `Ctrl+D` duplicate ·
`Ctrl+Z` / `Ctrl+Y` undo / redo · `+` / `−` zoom · `M` mute the selected
clip's track · `←` / `→` nudge · `Ctrl+Enter` render.

## How it is built

```
app.py                    stdlib HTTP server: the page + two JSON endpoints
mealeditor/engine.py      the engine: library, clips, effects, meal grouping,
                          tolerance check, timing notes, plan export
web/index.html            the editor page (vanilla HTML/CSS/JS, no build step)
data/ingredients.json     the library, one track per ingredient
data/prep_effects.json    cooking methods + finishes as stackable effects; transition kinds
data/demo_days.json       the demo projects with their profiles
tests/test_engine.py      unit tests (python -m pytest)
```

The page only draws. Every number on screen comes from `POST /api/render`,
which runs the deterministic engine — one source of truth for the maths.

### The macro model

Each ingredient carries kcal, protein, carbs and fat per 100 g. A clip's
macros are ingredient × portion, then each effect in order: multiply the
clip's own macros by `scale` (grilled: fat × 0.9), add `per_100g` deltas
(pan-fried: +6 g fat per 100 g), add `flat` deltas once per clip (cheese
topping: +5 g protein, +7 g fat). Calories follow the grams through the
Atwater factors (4 / 4 / 9), so an effect can never change fat without
changing kcal. Ingredient values are typical reference figures, rounded;
treat them as a planning aid, not a lab report.

## Tests

```bash
pip install pytest
python -m pytest tests -q
```
