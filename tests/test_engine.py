"""Unit tests for the meal editor engine and the API handlers."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mealeditor import engine as me
from mealeditor.engine import Profile, Targets


@pytest.fixture(scope="module")
def lib():
    return me.Library.load()


@pytest.fixture()
def alex():
    return Profile(name="Alex", calories_kcal=2400, protein_g=160, dislikes=["lentil"],
                   usual_bedtime="23:00", training_tomorrow=True)


@pytest.fixture()
def maya():
    return Profile(name="Maya", calories_kcal=1800, protein_g=120, restrictions=["lactose"],
                   dislikes=["jerky"], usual_bedtime="22:30")


def clip(ingredient, grams, start=12 * 60, effects=(), track=None, cid=None, duration=30):
    return {"id": cid or f"{ingredient}-{start}", "ingredient": ingredient, "track": track,
            "start": start, "duration": duration, "grams": grams, "effects": list(effects)}


def project(lib, clips, transitions=(), muted=()):
    return me.project_from_dict({"clips": clips, "transitions": list(transitions), "muted_tracks": list(muted)}, lib)


# ---------------------------------------------------------------- library --

def test_library_is_consistent(lib):
    assert [t["id"] for t in lib.tracks] == ["protein", "carbs", "veg", "fats", "extras"]
    assert len(lib.ingredients) >= 60
    for ing in lib.ingredients.values():
        assert ing.track in lib.track_ids, ing.id
        assert ing.default_g > 0
    assert {e.group for e in lib.effects.values()} == {"cook", "finish"}
    assert set(lib.transition_ids) == {"training", "walk", "fast", "sleep"}


def test_project_from_dict_rejects_unknown_ids(lib):
    with pytest.raises(ValueError, match="unknown ingredient"):
        project(lib, [clip("unicorn_steak", 100)])
    with pytest.raises(ValueError, match="unknown effect"):
        project(lib, [clip("chicken_breast", 100, effects=["microwaved"])])
    with pytest.raises(ValueError, match="unknown transition"):
        project(lib, [], transitions=[{"id": "t", "kind": "nap", "start": 800, "duration": 30}])


def test_track_defaults_to_the_ingredient_lane(lib):
    assert project(lib, [clip("broccoli", 100)]).clips[0].track == "veg"


def test_profile_from_dict_normalises_and_derives_targets():
    p = me.profile_from_dict({"name": "Sam", "calories_kcal": "2000", "protein_g": 150,
                              "restrictions": ["Lactose", " "], "dislikes": ["Jerky"]})
    assert p.targets == Targets(2000, 150)
    assert p.restrictions == ["lactose"] and p.dislikes == ["jerky"]
    assert me.profile_from_dict(None).targets is None
    assert me.profile_from_dict({"calories_kcal": 2000}).targets is None   # both needed


# ------------------------------------------------------------------- math --

def test_clip_macros_scale_with_portion(lib):
    m = me.clip_macros(project(lib, [clip("chicken_breast", 150)]).clips[0], lib)
    assert m.kcal == pytest.approx(165 * 1.5)
    assert m.protein_g == pytest.approx(31 * 1.5)


def test_grilled_effect_scales_fat_and_calories(lib):
    plain = me.clip_macros(project(lib, [clip("salmon", 100)]).clips[0], lib)
    grilled = me.clip_macros(project(lib, [clip("salmon", 100, effects=["grilled"])]).clips[0], lib)
    assert grilled.fat_g == pytest.approx(plain.fat_g * 0.9)
    assert grilled.kcal == pytest.approx(plain.kcal - plain.fat_g * 0.1 * 9)


def test_pan_fried_adds_oil_per_100g(lib):
    plain = me.clip_macros(project(lib, [clip("tofu", 200)]).clips[0], lib)
    fried = me.clip_macros(project(lib, [clip("tofu", 200, effects=["pan_fried"])]).clips[0], lib)
    assert fried.fat_g == pytest.approx(plain.fat_g + 6 * 2)
    assert fried.kcal == pytest.approx(plain.kcal + 6 * 2 * 9)


def test_flat_finish_effect_is_per_clip_not_per_gram(lib):
    def delta(grams):
        with_fx = me.clip_macros(project(lib, [clip("pasta", grams, effects=["cheese_topping"])]).clips[0], lib)
        plain = me.clip_macros(project(lib, [clip("pasta", grams)]).clips[0], lib)
        return with_fx.kcal - plain.kcal
    assert delta(100) == pytest.approx(delta(300)) == pytest.approx(5 * 4 + 7 * 9)


def test_effects_stack_in_order(lib):
    c = project(lib, [clip("chicken_breast", 100, effects=["grilled", "breaded", "oil_drizzle"])]).clips[0]
    m = me.clip_macros(c, lib)
    assert m.fat_g == pytest.approx(3.6 * 0.9 + 2 + 4.5)
    assert m.carbs_g == pytest.approx(10)
    assert "gluten" in me.clip_tags(c, lib)


# ------------------------------------------------------------ meal grouping --

def test_clips_close_together_form_one_meal(lib):
    p = project(lib, [clip("oats", 60, start=450), clip("whey", 30, start=470), clip("chicken_breast", 150, start=750)])
    assert [len(m) for m in me.group_meals(p.clips)] == [2, 1]
    r = me.render(p, lib)
    assert [m["label"] for m in r["meals"]] == ["breakfast", "lunch"]
    assert r["meals"][0]["time"] == "07:30–08:20"


def test_repeated_slot_labels_are_numbered(lib):
    p = project(lib, [clip("apple", 150, start=15 * 60), clip("banana", 120, start=16 * 60 + 30)])
    assert [m["label"] for m in me.render(p, lib)["meals"]] == ["snack", "snack 2"]


def test_items_within_a_meal_follow_track_order(lib):
    p = project(lib, [clip("olive_oil", 10, start=750), clip("broccoli", 100, start=750), clip("chicken_breast", 150, start=750)])
    assert [i["track"] for i in me.render(p, lib)["meals"][0]["items"]] == ["protein", "veg", "fats"]


def test_muted_track_is_left_out_of_totals(lib):
    clips = [clip("chicken_breast", 100, start=750), clip("olive_oil", 20, start=750)]
    full = me.render(project(lib, clips), lib)
    muted = me.render(project(lib, clips, muted=["fats"]), lib)
    assert muted["totals"]["kcal"] == pytest.approx(full["totals"]["kcal"] - 884 * 0.2, abs=1)
    assert any("muted" in n["text"] for n in muted["notes"])
    assert muted["clip_count"] == 1


# ---------------------------------------------------------- profile checks --

def test_restriction_and_dislike_warnings(lib, maya, alex):
    r = me.render(project(lib, [clip("skyr", 200), clip("pasta", 100, effects=["creamy_sauce"])]), lib, maya)
    items = r["meals"][0]["items"]
    assert any("lactose" in w for w in items[0]["warnings"])
    assert any("lactose" in w for w in items[1]["warnings"])       # from the effect's tags
    assert any(n["level"] == "warn" and "clash" in n["text"] for n in r["notes"])
    r = me.render(project(lib, [clip("lentils", 200)]), lib, alex)
    assert any("dislikes" in w for w in r["meals"][0]["items"][0]["warnings"])


def test_tolerance_check_uses_the_profile_targets(lib):
    prof = Profile(name="T", calories_kcal=900, protein_g=160)
    # 2 × 250 g chicken = 825 kcal / 155 g protein → within ±150 / ±10
    p = project(lib, [clip("chicken_breast", 250, start=750, cid="a"), clip("chicken_breast", 250, start=1140, cid="b")])
    r = me.render(p, lib, prof)
    assert r["within_tolerance"] is True
    assert r["gaps"] == {"kcal": -75, "protein_g": -5}
    assert any(n["level"] == "ok" for n in r["notes"])
    p.clips.append(me.Clip(id="c", ingredient="ice_cream", track="extras", start=1200, grams=200))
    r = me.render(p, lib, prof)
    assert r["within_tolerance"] is False
    assert any("over target" in n["text"] for n in r["notes"])


def test_no_targets_yields_an_info_note_not_a_verdict(lib):
    r = me.render(project(lib, [clip("apple", 150)]), lib, Profile())
    assert r["within_tolerance"] is None and r["gaps"] is None
    assert any("No daily targets" in n["text"] for n in r["notes"])


def test_light_protein_meal_is_noted(lib):
    r = me.render(project(lib, [clip("apple", 150, start=960)]), lib)
    assert any("Light on protein" in n for n in r["meals"][0]["notes"])


# ----------------------------------------------------------------- timing --

def test_sleep_transition_flags_a_late_meal(lib, alex):
    late = [clip("salmon", 150, start=21 * 60 + 30)]
    r = me.render(project(lib, late, transitions=[{"id": "s", "kind": "sleep", "start": 23 * 60, "duration": 60}]), lib, alex)
    assert any("before bedtime" in n["text"] for n in r["notes"])
    r = me.render(project(lib, late), lib, alex)                 # profile bedtime is the fallback
    assert any("bedtime (23:00)" in n["text"] for n in r["notes"])
    r = me.render(project(lib, [clip("salmon", 150, start=18 * 60)]), lib, alex)
    assert not any("bedtime" in n["text"] for n in r["notes"])


def test_training_transition_checks_fuel_and_recovery(lib):
    training = [{"id": "t", "kind": "training", "start": 17 * 60 + 30, "duration": 60}]
    r = me.render(project(lib, [clip("cucumber", 100, start=16 * 60)], transitions=training), lib)
    texts = [n["text"] for n in r["notes"]]
    assert any("before training" in t for t in texts)
    assert any("after training" in t for t in texts)
    fuelled = [clip("banana", 150, start=16 * 60, cid="pre"), clip("chicken_breast", 150, start=19 * 60, cid="post")]
    r = me.render(project(lib, fuelled, transitions=training), lib)
    assert not any("training" in n["text"] for n in r["notes"])


def test_fasting_window_overlap_is_a_warning(lib):
    fast = [{"id": "f", "kind": "fast", "start": 6 * 60, "duration": 6 * 60}]
    r = me.render(project(lib, [clip("oats", 60, start=8 * 60)], transitions=fast), lib)
    assert any(n["level"] == "warn" and "fasting window" in n["text"] for n in r["notes"])


# ------------------------------------------------------------ export / demos --

def test_export_lists_every_meal_with_its_totals(lib, alex):
    demos = me.load_demo_days()
    r = me.render(me.project_from_dict(demos["muscle-gain"], lib), lib, alex)
    text = me.export_plan_markdown(r, "Alex", "lean muscle gain")
    assert "Client: Alex" in text and "- Daily calories: 2400 kcal" in text
    for meal in r["meals"]:
        line = next(l for l in text.splitlines() if l.startswith(f"- {meal['label'].capitalize()}:"))
        assert f"{meal['totals']['kcal']} kcal, {int(round(meal['totals']['protein_g']))} g protein" in line


def test_demo_days_land_within_tolerance_for_their_own_profiles(lib):
    for key, demo in me.load_demo_days().items():
        prof = me.profile_from_dict(demo["profile"])
        r = me.render(me.project_from_dict(demo, lib), lib, prof)
        assert r["within_tolerance"], (key, r["gaps"])
        assert not any(i["warnings"] for m in r["meals"] for i in m["items"]), key


# -------------------------------------------------------------------- app --

def test_app_handlers():
    import app
    payload = app.library_payload()
    assert set(payload["demos"]) == {"muscle-gain", "fat-loss"}
    assert payload["tolerance"] == {"calories_kcal": 150, "protein_g": 10}
    assert len(payload["ingredients"]) == len(app.LIB.ingredients)

    demo = payload["demos"]["fat-loss"]
    out = app.handle_render({"profile": demo["profile"], "project": demo})
    assert out["targets"] == {"calories_kcal": 1800, "protein_g": 120}
    assert out["within_tolerance"] is True
    assert out["export_markdown"].startswith("# Nutrition Plan")
    assert "Client: Maya" in out["export_markdown"]

    out = app.handle_render({"project": {"clips": [{"ingredient": "apple", "start": 600}]}})
    assert out["within_tolerance"] is None
    with pytest.raises(ValueError):
        app.handle_render({"project": {"clips": [{"ingredient": "nope"}]}})
