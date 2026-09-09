"""Meal editor engine — a day of eating treated like an editing timeline.

The metaphor comes straight from video/audio editors:

  library   -> ingredients (data/ingredients.json), the media bin
  tracks    -> component lanes: protein / carbs / vegetables / fats / extras
  clips     -> an ingredient portion placed at a time of day; the clip's
               length is the eating window, its grams are the portion
  effects   -> preparation methods and finishes stacked on a clip
               (data/prep_effects.json): grilled, pan-fried, cheese topping…
  transitions -> what happens *between* meals: training, a walk, a fasting
               window, sleep — they change the timing advice, never the plan
  render    -> the finished day: meals with the macro math shown, the
               tolerance check against the daily targets, timing notes,
               and a plain-text plan document

Everything here is deterministic and stdlib-only: no model is ever in the
loop for arithmetic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_TOLERANCE = {"calories_kcal": 150, "protein_g": 10}

DAY_START_MIN = 6 * 60      # the ruler runs 06:00 -> 24:00
DAY_END_MIN = 24 * 60
MEAL_GAP_MIN = 30           # clips closer than this belong to the same meal
BEDTIME_BUFFER_MIN = 120    # sleep agent: finish eating 2-3 h before bed
PRE_TRAINING_WINDOW_MIN = 180
POST_TRAINING_WINDOW_MIN = 120
LIGHT_PROTEIN_G = 20        # a meal under this is flagged "light on protein"
CARB_FUEL_G = 30            # carbs needed in the pre-training window

ATWATER = {"protein_g": 4.0, "carbs_g": 4.0, "fat_g": 9.0}

# (upper bound in minutes, label) — the slot a meal gets from its start time
SLOT_BOUNDS = [(10 * 60 + 30, "breakfast"), (14 * 60 + 30, "lunch"),
               (17 * 60 + 30, "snack"), (21 * 60 + 30, "dinner"), (DAY_END_MIN + 1, "late snack")]


# ----------------------------------------------------------------- models --

@dataclass(frozen=True)
class Targets:
    """Daily targets the day is checked against."""

    calories_kcal: int
    protein_g: int


@dataclass
class Profile:
    """Who the day is for: targets plus the things the checks filter against."""

    name: str = "Client"
    goal: str = ""
    calories_kcal: int = 0            # 0 = no target set
    protein_g: int = 0
    restrictions: list[str] = field(default_factory=list)   # ingredient tags to avoid, e.g. ["lactose"]
    dislikes: list[str] = field(default_factory=list)       # name fragments, e.g. ["lentil"]
    usual_bedtime: str = "23:00"
    training_tomorrow: bool = False

    @property
    def targets(self) -> Targets | None:
        if self.calories_kcal > 0 and self.protein_g > 0:
            return Targets(int(self.calories_kcal), int(self.protein_g))
        return None


def profile_from_dict(data: dict | None) -> Profile:
    d = data or {}
    return Profile(
        name=str(d.get("name") or "Client"),
        goal=str(d.get("goal") or ""),
        calories_kcal=int(d.get("calories_kcal") or 0),
        protein_g=int(d.get("protein_g") or 0),
        restrictions=[str(r).strip().lower() for r in d.get("restrictions", []) if str(r).strip()],
        dislikes=[str(x).strip().lower() for x in d.get("dislikes", []) if str(x).strip()],
        usual_bedtime=str(d.get("usual_bedtime") or "23:00"),
        training_tomorrow=bool(d.get("training_tomorrow", False)),
    )


@dataclass(frozen=True)
class Ingredient:
    id: str
    name: str
    track: str
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    default_g: int
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Effect:
    id: str
    name: str
    group: str                       # "cook" (one per clip) or "finish" (stackable)
    scale: dict = field(default_factory=dict)
    per_100g: dict = field(default_factory=dict)
    flat: dict = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    hint: str = ""


@dataclass
class Macros:
    kcal: float = 0.0
    protein_g: float = 0.0
    carbs_g: float = 0.0
    fat_g: float = 0.0

    def __add__(self, other: "Macros") -> "Macros":
        return Macros(self.kcal + other.kcal, self.protein_g + other.protein_g,
                      self.carbs_g + other.carbs_g, self.fat_g + other.fat_g)

    def rounded(self) -> dict:
        return {"kcal": int(round(self.kcal)), "protein_g": round(self.protein_g, 1),
                "carbs_g": round(self.carbs_g, 1), "fat_g": round(self.fat_g, 1)}


@dataclass
class Clip:
    id: str
    ingredient: str
    track: str
    start: int            # minutes since midnight
    duration: int = 20    # minutes — the eating window
    grams: float = 100.0
    effects: list[str] = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.start + self.duration


@dataclass
class Transition:
    id: str
    kind: str             # training | walk | fast | sleep
    start: int
    duration: int

    @property
    def end(self) -> int:
        return self.start + self.duration


@dataclass
class Project:
    clips: list[Clip] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    muted_tracks: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- library --

class Library:
    """The media bin: ingredients, effects, tracks and transition kinds."""

    def __init__(self, ingredients: list[Ingredient], effects: list[Effect],
                 tracks: list[dict], transition_kinds: list[dict]):
        self.ingredients = {i.id: i for i in ingredients}
        self.effects = {e.id: e for e in effects}
        self.tracks = tracks
        self.track_ids = [t["id"] for t in tracks]
        self.transition_kinds = transition_kinds
        self.transition_ids = [t["id"] for t in transition_kinds]

    @classmethod
    def load(cls, ingredients_path: Path | str = DATA_DIR / "ingredients.json",
             effects_path: Path | str = DATA_DIR / "prep_effects.json") -> "Library":
        ing_raw = json.loads(Path(ingredients_path).read_text(encoding="utf-8"))
        eff_raw = json.loads(Path(effects_path).read_text(encoding="utf-8"))
        ingredients = [
            Ingredient(id=i["id"], name=i["name"], track=i["track"], kcal=float(i["kcal"]),
                       protein_g=float(i["protein_g"]), carbs_g=float(i["carbs_g"]),
                       fat_g=float(i["fat_g"]), default_g=int(i["default_g"]),
                       tags=tuple(i.get("tags", [])))
            for i in ing_raw["ingredients"]
        ]
        effects = [
            Effect(id=e["id"], name=e["name"], group=e["group"], scale=dict(e.get("scale", {})),
                   per_100g=dict(e.get("per_100g", {})), flat=dict(e.get("flat", {})),
                   tags=tuple(e.get("tags", [])), hint=e.get("hint", ""))
            for e in eff_raw["effects"]
        ]
        return cls(ingredients, effects, ing_raw["tracks"], eff_raw["transitions"])

    def payload(self) -> dict:
        """JSON-ready copy for the web editor."""
        return {
            "tracks": self.tracks,
            "transition_kinds": self.transition_kinds,
            "ingredients": [
                {"id": i.id, "name": i.name, "track": i.track, "kcal": i.kcal,
                 "protein_g": i.protein_g, "carbs_g": i.carbs_g, "fat_g": i.fat_g,
                 "default_g": i.default_g, "tags": list(i.tags)}
                for i in self.ingredients.values()
            ],
            "effects": [
                {"id": e.id, "name": e.name, "group": e.group, "hint": e.hint, "tags": list(e.tags)}
                for e in self.effects.values()
            ],
            "day_start": DAY_START_MIN,
            "day_end": DAY_END_MIN,
        }


def project_from_dict(data: dict, lib: Library) -> Project:
    """Build a Project from the editor's JSON, refusing anything the library
    does not know — an unknown ingredient must not silently become 0 kcal."""
    clips: list[Clip] = []
    for c in data.get("clips", []):
        ing = c.get("ingredient")
        if ing not in lib.ingredients:
            raise ValueError(f"unknown ingredient: {ing!r}")
        track = c.get("track") or lib.ingredients[ing].track
        if track not in lib.track_ids:
            raise ValueError(f"unknown track: {track!r}")
        effects = list(c.get("effects", []))
        for e in effects:
            if e not in lib.effects:
                raise ValueError(f"unknown effect: {e!r}")
        grams = float(c.get("grams", lib.ingredients[ing].default_g))
        if grams < 0:
            raise ValueError("grams must be >= 0")
        clips.append(Clip(id=str(c.get("id") or f"c{len(clips) + 1}"), ingredient=ing, track=track,
                          start=int(c.get("start", DAY_START_MIN)),
                          duration=max(5, int(c.get("duration", 20))),
                          grams=grams, effects=effects))
    transitions: list[Transition] = []
    for t in data.get("transitions", []):
        kind = t.get("kind")
        if kind not in lib.transition_ids:
            raise ValueError(f"unknown transition: {kind!r}")
        transitions.append(Transition(id=str(t.get("id") or f"t{len(transitions) + 1}"), kind=kind,
                                      start=int(t.get("start", DAY_START_MIN)),
                                      duration=max(5, int(t.get("duration", 30)))))
    muted = [m for m in data.get("muted_tracks", []) if m in lib.track_ids]
    return Project(clips=clips, transitions=transitions, muted_tracks=muted)


# ------------------------------------------------------------------- math --

def clip_macros(clip: Clip, lib: Library) -> Macros:
    """Ingredient × portion, then each effect in order: scale the clip's own
    macros, add per-100 g deltas, add flat deltas. Calories follow the grams
    via the Atwater factors so an effect never changes fat without changing
    kcal."""
    ing = lib.ingredients[clip.ingredient]
    factor = clip.grams / 100.0
    m = Macros(ing.kcal * factor, ing.protein_g * factor, ing.carbs_g * factor, ing.fat_g * factor)
    for eff_id in clip.effects:
        eff = lib.effects[eff_id]
        for key, mult in eff.scale.items():
            before = getattr(m, key)
            after = before * mult
            setattr(m, key, after)
            m.kcal += (after - before) * ATWATER[key]
        for key, per100 in eff.per_100g.items():
            _add(m, key, per100 * factor)
        for key, value in eff.flat.items():
            _add(m, key, value)
    return m


def _add(m: Macros, key: str, value: float) -> None:
    if key == "kcal":
        m.kcal += value
        return
    setattr(m, key, getattr(m, key) + value)
    m.kcal += value * ATWATER[key]


def clip_tags(clip: Clip, lib: Library) -> set[str]:
    tags = set(lib.ingredients[clip.ingredient].tags)
    for e in clip.effects:
        tags.update(lib.effects[e].tags)
    return tags


def slot_label(start_min: int) -> str:
    for bound, label in SLOT_BOUNDS:
        if start_min < bound:
            return label
    return "late snack"


def group_meals(clips: list[Clip]) -> list[list[Clip]]:
    """Clips that start within MEAL_GAP_MIN of the running meal's end form
    one meal — the way adjacent clips read as one scene."""
    meals: list[list[Clip]] = []
    meal_end = None
    for clip in sorted(clips, key=lambda c: (c.start, c.track, c.id)):
        if meal_end is None or clip.start > meal_end + MEAL_GAP_MIN:
            meals.append([clip])
            meal_end = clip.end
        else:
            meals[-1].append(clip)
            meal_end = max(meal_end, clip.end)
    return meals


def hhmm(minutes: int) -> str:
    minutes = max(0, int(minutes))
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _parse_hhmm(text: str) -> int | None:
    try:
        h, m = text.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


# ----------------------------------------------------------------- render --

def render(project: Project, lib: Library, profile: Profile | None = None,
           targets: Targets | None = None, tolerance: dict | None = None) -> dict:
    """The finished day. Returns a JSON-ready dict: meals with the macro math
    shown, day totals, the tolerance check, warnings per clip, and timing
    notes derived from the transitions. Targets default to the profile's."""
    tolerance = tolerance or DEFAULT_TOLERANCE
    if targets is None and profile is not None:
        targets = profile.targets
    restrictions = [r.lower() for r in (profile.restrictions if profile else [])]
    dislikes = [d.lower() for d in (profile.dislikes if profile else [])]
    active = [c for c in project.clips if c.track not in project.muted_tracks]

    meals_out: list[dict] = []
    seen_labels: dict[str, int] = {}
    day = Macros()
    for group in group_meals(active):
        start = min(c.start for c in group)
        end = max(c.end for c in group)
        base = slot_label(start)
        seen_labels[base] = seen_labels.get(base, 0) + 1
        label = base if seen_labels[base] == 1 else f"{base} {seen_labels[base]}"
        items: list[dict] = []
        total = Macros()
        order = {t: i for i, t in enumerate(lib.track_ids)}
        for clip in sorted(group, key=lambda c: (order.get(c.track, 99), c.start, c.id)):
            ing = lib.ingredients[clip.ingredient]
            m = clip_macros(clip, lib)
            total = total + m
            warnings: list[str] = []
            tags = clip_tags(clip, lib)
            for r in restrictions:
                if r in tags:
                    warnings.append(f"carries '{r}' — this plan avoids it")
            for d in dislikes:
                if d in ing.name.lower():
                    warnings.append(f"'{d}' is on the dislikes list")
            items.append({
                "clip_id": clip.id, "ingredient": ing.id, "name": ing.name, "track": clip.track,
                "grams": round(clip.grams, 1), "start": clip.start, "end": clip.end,
                "effects": [lib.effects[e].name for e in clip.effects],
                "macros": m.rounded(), "warnings": warnings, "tags": sorted(tags),
            })
        day = day + total
        notes: list[str] = []
        if total.protein_g < LIGHT_PROTEIN_G:
            notes.append(f"Light on protein ({int(round(total.protein_g))} g) — a protein clip here keeps the daily anchor in reach.")
        meals_out.append({
            "label": label, "start": start, "end": end, "time": f"{hhmm(start)}–{hhmm(end)}",
            "items": items, "totals": total.rounded(), "notes": notes,
        })

    day_notes: list[dict] = []
    warnings_total = sum(len(i["warnings"]) for meal in meals_out for i in meal["items"])
    if not meals_out:
        day_notes.append({"level": "info", "text": "The timeline is empty — drop ingredients from the library onto a track."})
    for track in project.muted_tracks:
        name = next((t["name"] for t in lib.tracks if t["id"] == track), track)
        day_notes.append({"level": "info", "text": f"Track '{name}' is muted — its clips are left out of the totals."})
    if warnings_total:
        day_notes.append({"level": "warn", "text": f"{warnings_total} clip(s) clash with the plan's restrictions or dislikes — see the marked items."})

    gaps = None
    within = None
    if targets:
        kcal_gap = int(round(day.kcal)) - targets.calories_kcal
        prot_gap = int(round(day.protein_g)) - targets.protein_g
        gaps = {"kcal": kcal_gap, "protein_g": prot_gap}
        within = abs(kcal_gap) <= tolerance["calories_kcal"] and abs(prot_gap) <= tolerance["protein_g"]
        band = f"±{tolerance['calories_kcal']} kcal / ±{tolerance['protein_g']} g protein"
        if within:
            day_notes.append({"level": "ok", "text": f"The day lands within tolerance ({band})."})
        else:
            if prot_gap < -tolerance["protein_g"]:
                day_notes.append({"level": "warn", "text": f"Protein is {-prot_gap} g short of the daily anchor — lengthen a protein clip or add one."})
            elif prot_gap > tolerance["protein_g"]:
                day_notes.append({"level": "info", "text": f"Protein runs {prot_gap} g over target — fine for the anchor, but it counts toward calories."})
            if kcal_gap > tolerance["calories_kcal"]:
                day_notes.append({"level": "warn", "text": f"The day runs {kcal_gap} kcal over target, beyond the {band} band — trim a portion, or let calories average out over a 3–7 day window."})
            elif kcal_gap < -tolerance["calories_kcal"]:
                day_notes.append({"level": "warn", "text": f"The day runs {-kcal_gap} kcal under target, beyond the {band} band — add or lengthen a clip; an under-fuelled day is not the goal."})
    else:
        day_notes.append({"level": "info", "text": "No daily targets set — add calorie and protein targets to the client profile to check the day."})

    day_notes.extend(_timing_notes(project, meals_out, profile))

    return {
        "meals": meals_out,
        "totals": day.rounded(),
        "targets": None if not targets else {"calories_kcal": targets.calories_kcal, "protein_g": targets.protein_g},
        "gaps": gaps,
        "within_tolerance": within,
        "tolerance": {"calories_kcal": tolerance["calories_kcal"], "protein_g": tolerance["protein_g"]},
        "notes": day_notes,
        "muted_tracks": list(project.muted_tracks),
        "clip_count": len(active),
    }


def _timing_notes(project: Project, meals: list[dict], profile: Profile | None) -> list[dict]:
    """What the transitions say about *when* the meals sit in the day."""
    notes: list[dict] = []
    sleeps = [t for t in project.transitions if t.kind == "sleep"]
    bedtime = min((t.start for t in sleeps), default=None)
    if bedtime is None and profile:
        bedtime = _parse_hhmm(profile.usual_bedtime)
    if bedtime is not None:
        for meal in meals:
            if meal["end"] > bedtime - BEDTIME_BUFFER_MIN:
                notes.append({"level": "info", "text": (
                    f"'{meal['label']}' ends at {hhmm(meal['end'])}, under 2 h before bedtime ({hhmm(bedtime)}) — "
                    "aim to finish 2–3 h before bed and keep it lighter on fat so it doesn't cut into deep sleep.")})
            if meal["end"] > bedtime - 3 * 60 and any("alcohol" in i["tags"] for i in meal["items"]):
                notes.append({"level": "info", "text": (
                    f"Alcohol in '{meal['label']}' within 3 h of bedtime — easy on it, it fragments sleep.")})

    for t in project.transitions:
        if t.kind == "training":
            fuel = [m for m in meals if t.start - PRE_TRAINING_WINDOW_MIN <= m["end"] <= t.start
                    and m["totals"]["carbs_g"] >= CARB_FUEL_G]
            if not fuel:
                notes.append({"level": "info", "text": (
                    f"Nothing with ≥{CARB_FUEL_G} g carbs in the 3 h before training at {hhmm(t.start)} — "
                    "a carb clip there fuels the session.")})
            recovery = [m for m in meals if t.end <= m["start"] <= t.end + POST_TRAINING_WINDOW_MIN
                        and m["totals"]["protein_g"] >= LIGHT_PROTEIN_G]
            if not recovery:
                notes.append({"level": "info", "text": (
                    f"No meal with ≥{LIGHT_PROTEIN_G} g protein in the 2 h after training ends ({hhmm(t.end)}) — "
                    "anchor recovery with a protein clip.")})
            for m in meals:
                if m["start"] < t.end and m["end"] > t.start:
                    notes.append({"level": "warn", "text": f"'{m['label']}' overlaps the training block {hhmm(t.start)}–{hhmm(t.end)}."})
        elif t.kind == "fast":
            for m in meals:
                if m["start"] < t.end and m["end"] > t.start:
                    notes.append({"level": "warn", "text": (
                        f"'{m['label']}' sits inside the fasting window {hhmm(t.start)}–{hhmm(t.end)} — move one or the other.")})
        elif t.kind == "sleep":
            for m in meals:
                if m["start"] < t.end and m["end"] > t.start:
                    notes.append({"level": "warn", "text": f"'{m['label']}' overlaps sleep starting {hhmm(t.start)}."})
    if profile and profile.training_tomorrow and meals:
        last = meals[-1]
        if last["totals"]["protein_g"] < LIGHT_PROTEIN_G:
            notes.append({"level": "info", "text": (
                f"Training tomorrow — the last meal ('{last['label']}') is light on protein; "
                "anchor tonight's protein so the session doesn't feel flat.")})
    return notes


# ----------------------------------------------------------------- export --

def export_plan_markdown(rendered: dict, client_name: str = "Client", goal: str = "") -> str:
    """Render the day as a plain-text plan: a targets header plus one bullet
    per meal with its ingredients, kcal and protein, then totals and notes."""
    lines = ["# Nutrition Plan — built in the meal editor", "",
             f"Client: {client_name}"]
    if goal:
        lines.append(f"Goal: {goal}")
    lines.append("")
    t = rendered.get("targets")
    if t:
        lines += ["## Daily targets", "",
                  f"- Daily calories: {t['calories_kcal']} kcal",
                  f"- Daily protein: {t['protein_g']} g", ""]
    lines += ["## Baseline day", ""]
    for meal in rendered["meals"]:
        parts = []
        for item in meal["items"]:
            desc = f"{item['name'].lower()} {int(round(item['grams']))} g"
            if item["effects"]:
                desc += f" ({', '.join(e.lower() for e in item['effects'])})"
            parts.append(desc)
        tot = meal["totals"]
        label = meal["label"].capitalize()
        lines.append(f"- {label}: {', '.join(parts)} — {tot['kcal']} kcal, {int(round(tot['protein_g']))} g protein")
    tot = rendered["totals"]
    lines += ["", "## Day totals", "",
              f"{tot['kcal']} kcal, {int(round(tot['protein_g']))} g protein, "
              f"{int(round(tot['carbs_g']))} g carbs, {int(round(tot['fat_g']))} g fat"]
    if rendered.get("gaps"):
        g = rendered["gaps"]
        status = "within tolerance" if rendered.get("within_tolerance") else "outside tolerance"
        lines.append(f"Against target — {g['kcal']:+d} kcal, {g['protein_g']:+d} g protein ({status})")
    timing = [n["text"] for n in rendered.get("notes", []) if n["level"] != "ok"]
    if timing:
        lines += ["", "## Notes", ""]
        lines += [f"- {n}" for n in timing]
    return "\n".join(lines) + "\n"


def load_demo_days(path: Path | str = DATA_DIR / "demo_days.json") -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}
