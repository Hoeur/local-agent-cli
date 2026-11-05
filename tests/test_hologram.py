"""Tests for the holographic core engine: state machine, projection, rasterizers, governor and
capability detection. Skipped when Textual (and with it Rich) is not installed."""
import importlib.util
import unittest

if importlib.util.find_spec("textual") is None:  # The UI is an optional extra: pip install -e ".[tui]"
    raise unittest.SkipTest("Textual is not installed")

from local_agent.tui import hologram as h
from local_agent.tui.theme import PALETTES

PALETTE = PALETTES["neon"]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, director, seconds, fps=30, palette=PALETTE):
        for _ in range(int(seconds * fps)):
            self.t += 1 / fps
            director.step(self.t, palette)


def braille_density(cols, rows):
    return min(cols * 2, rows * 4) / 2 / h.EXTENT


class DirectorTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.director = h.Director(self.clock)

    def test_states_ease_instead_of_jumping(self):
        d = self.director
        self.clock.advance(d, 0.5)
        idle = d.params["energy"]
        d.set_state("thinking")
        self.clock.advance(d, 0.1)
        partway = d.params["energy"]
        self.assertGreater(partway, idle)
        self.assertLess(partway, h.PROFILES["thinking"]["energy"] - 0.1)
        self.clock.advance(d, 2.0)
        self.assertAlmostEqual(d.params["energy"], h.PROFILES["thinking"]["energy"], places=2)

    def test_static_step_snaps_to_target(self):
        d = self.director
        d.set_state("generating")
        d.step(1.0, PALETTE, motion=False)
        self.assertEqual(d.params, h.PROFILES["generating"])

    def test_phase_is_continuous_when_speed_changes(self):
        d = self.director
        self.clock.advance(d, 1.0)
        before = d.phase["spin"]
        d.set_state("thinking")
        self.clock.advance(d, 1 / 30)
        self.assertLess(d.phase["spin"] - before, 0.1)  # No jump, just a slightly faster step.

    def test_flashes_expire_and_tint(self):
        d = self.director
        self.clock.advance(d, 0.3)
        d.flash("success")
        self.clock.advance(d, 0.2)
        self.assertEqual(d.flash_age()[0], "success")
        self.assertEqual(d.label(), "complete")
        self.assertGreater(d.tint_amount, 0.3)
        self.clock.advance(d, 1.5)
        self.assertIsNone(d.flash_age()[0])
        self.assertLess(d.tint_amount, 0.05)
        with self.assertRaises(ValueError):
            d.flash("sparkles")
        with self.assertRaises(ValueError):
            d.set_state("dancing")

    def test_waves_merge_bursts_and_fade(self):
        d = self.director
        for _ in range(20):
            d.wave(0.5)  # All at the same instant: one merged wave.
        self.assertEqual(len(d.waves), 1)
        self.assertLessEqual(d.waves[0][1], 1.4)
        for _ in range(10):
            self.clock.t += 0.2
            d.wave(0.5)
        self.assertLessEqual(len(d.waves), 6)
        self.assertTrue(d.active())
        self.clock.advance(d, h.WAVE_LIFE + 0.1)
        self.assertEqual(d.waves, [])
        self.assertFalse(d.active())

    def test_tool_progress(self):
        d = self.director
        d.set_state("tool", "read_file")
        d.tool_round(2)
        self.assertLess(d.progress(), 0.05)
        self.clock.t += 5
        self.assertAlmostEqual(d.progress(), 0.85 / 2, places=1)  # The running call approaches 85%.
        self.assertLess(d.progress(), 0.85 / 2)
        d.tool_finished()
        d.tool_started_event()
        self.assertAlmostEqual(d.progress(), 0.5, places=2)
        d.tool_finished(error=True)
        self.assertEqual(d.progress(), 1.0)
        self.assertEqual(d.flash_age()[0], "glitch")
        d.set_state("idle")
        self.assertEqual((d.tools_total, d.tools_done), (0, 0))

    def test_voice_needs_live_audio(self):
        d = self.director
        self.assertEqual(d.effective_state(), "idle")
        d.set_audio_level(1.7)
        self.assertEqual(d.audio, 1.0)
        self.assertEqual(d.effective_state(), "voice")
        d.set_state("thinking")
        self.assertEqual(d.effective_state(), "thinking")  # Agent activity wins over voice.
        d.set_state("idle")
        self.clock.t += h.AUDIO_STALE + 0.1
        self.assertEqual(d.effective_state(), "idle")


class RenderTests(unittest.TestCase):
    def frame(self, state="idle", seconds=1.0, density=None, palette=PALETTE):
        clock = FakeClock()
        director = h.Director(clock)
        director.set_state(state)
        clock.advance(director, seconds, palette=palette)
        return h.Scene().frame(director, clock.t, palette, density or braille_density(30, 14))

    def test_braille_output_fits_and_uses_braille(self):
        for state in h.STATES:
            for cols, rows in ((30, 14), (47, 22), (18, 8)):
                rows_out = h.rasterize_braille(self.frame(state, density=braille_density(cols, rows)), cols, rows)
                self.assertEqual(len(rows_out), rows)
                for row in rows_out:
                    text = "".join(run[0] for run in row)
                    self.assertEqual(len(text), cols)
                    self.assertTrue(all(char == " " or 0x2800 <= ord(char) <= 0x28FF for char in text))
        lit = sum(run[0].count(c) for row in h.rasterize_braille(self.frame(), 30, 14) for run in row
                  for c in set(run[0]) if c != " ")
        self.assertGreater(lit, 40)

    def test_ascii_fallback_is_pure_ascii(self):
        frame = self.frame("thinking", density=min(40, 36) / 2 / h.EXTENT)
        rows_out = h.rasterize_ascii(frame, 40, 18)
        self.assertEqual(len(rows_out), 18)
        text = "".join(run[0] for row in rows_out for run in row)
        self.assertTrue(text.isascii())
        self.assertIn("@", text)  # The core.

    def test_tiny_and_empty_canvases_do_not_crash(self):
        for raster in h.RASTERIZERS.values():
            self.assertEqual(raster(self.frame(), 0, 0), [])
            self.assertEqual(len(raster(self.frame(), 1, 1)), 1)

    def test_points_stay_within_the_scene(self):
        for state in h.STATES:
            frame = self.frame(state, seconds=2.0)
            for x, y, depth, intensity, role, kind in frame.points:
                self.assertLessEqual(abs(x), h.EXTENT * 1.25)
                self.assertLessEqual(abs(y), h.EXTENT * 1.25)
                self.assertGreaterEqual(intensity, 0)

    def test_error_flash_distorts(self):
        clock = FakeClock()
        director = h.Director(clock)
        clock.advance(director, 0.3)
        director.flash("error")
        clock.advance(director, 0.1)
        frame = h.Scene().frame(director, clock.t, PALETTE, 14)
        self.assertGreater(frame.distort, 0.5)
        red, green, blue = h.effects.hex_rgb(frame.colors[h.FX])
        self.assertGreater(red, green + 60)  # Easing toward the error color.

    def test_every_theme_renders(self):
        for name, palette in PALETTES.items():
            rows_out = h.rasterize_braille(self.frame(palette=palette), 24, 11)
            self.assertEqual(len(rows_out), 11, name)

    def test_image_render_when_pillow_is_present(self):
        if importlib.util.find_spec("PIL") is None:
            self.skipTest("Pillow is not installed")
        image = h.render_image(self.frame("thinking"), 120, 110)
        self.assertEqual(image.size, (120, 110))
        self.assertEqual(image.getpixel((0, 0)), (7, 9, 15))  # Theme background is preserved exactly.


class GovernorTests(unittest.TestCase):
    def test_steps_down_under_load_and_recovers(self):
        governor = h.Governor(30)
        now = 0.0
        for _ in range(5):
            now += governor.interval()
            governor.record(0.02, now)  # 20 ms frames: too slow for 30 fps.
        self.assertLess(governor.fps, 30)
        for _ in range(400):
            now += governor.interval()
            governor.record(0.001, now)
        self.assertEqual(governor.fps, 30)

    def test_late_frames_count_as_strain(self):
        governor = h.Governor(30)
        now = 0.0
        for _ in range(10):
            now += 0.2  # The event loop delivers frames far later than asked.
            governor.record(0.001, now)
        self.assertLess(governor.fps, 30)

    def test_ceiling(self):
        governor = h.Governor(30)
        governor.set_ceiling(15)
        self.assertEqual(governor.fps, 15)
        governor.set_ceiling(4)
        self.assertLessEqual(governor.fps, 4)
        governor.set_ceiling(30)
        self.assertEqual(governor.fps, 30)  # Raising the ceiling jumps straight to it.


class CapabilityTests(unittest.TestCase):
    def test_choose_renderer(self):
        choose = h.choose_renderer
        self.assertEqual(choose("auto", {}, True, None)[0], "braille")
        self.assertEqual(choose("auto", {}, False, None)[0], "ascii")  # Legacy console glyphs.
        self.assertEqual(choose("auto", {"TERM": "linux"}, True, None)[0], "ascii")  # Linux VT font.
        self.assertEqual(choose("braille", {"TERM": "dumb"}, True, None)[0], "ascii")
        self.assertEqual(choose("ascii", {}, True, None)[0], "ascii")
        mode, reason = choose("image", {}, True, None)
        self.assertEqual(mode, "braille")
        self.assertIn("restart", reason)
        mode, reason = choose("image", {}, True, {"protocol": None, "error": "no graphics here"})
        self.assertEqual((mode, "no graphics here" in reason), ("braille", True))
        self.assertEqual(choose("image", {}, True, {"protocol": "sixel"})[0], "image")
        self.assertEqual(choose("auto", {"AGENT_HOLOGRAM": "ascii"}, True, None)[0], "ascii")


if __name__ == "__main__":
    unittest.main()
