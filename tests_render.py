"""Pixel coverage, dash continuity and drawing-layer lifetime; no Tk window or MIDI."""
import unittest
import threading
from unittest.mock import Mock, patch

from smooth_render import ANTIALIAS_AVAILABLE, RasterLayer, SmoothCanvas, blend, dashed_paths, painted


def memory_canvas():
    canvas = SmoothCanvas.__new__(SmoothCanvas)
    canvas._raster = canvas._paint_photo = canvas._paint_signature = None
    canvas.winfo_width = lambda: 48
    canvas.winfo_height = lambda: 32
    canvas.cget = lambda key: '#1b2128'
    return canvas


def white_rectangle(layer):
    layer.add('rounded', (2, 2, 46, 30), '#ffffff', radius=10)


def pixels(picture):
    return picture.get_flattened_data() if hasattr(picture, 'get_flattened_data') else picture.getdata()


class DashTests(unittest.TestCase):
    def test_dash_phase_does_not_restart_at_curve_sample_boundaries(self):
        paths = list(dashed_paths(((0, 0), (2, 0), (7, 0), (13, 0)), (3, 2)))
        self.assertEqual([(p[0], p[-1]) for p in paths],
                         [((0, 0), (3, 0)), ((5, 0), (8, 0)), ((10, 0), (13, 0))])

    def test_dash_distance_continues_around_a_corner(self):
        paths = list(dashed_paths(((0, 0), (4, 0), (4, 6)), (6, 2)))
        self.assertEqual(paths[0], ((0, 0), (4, 0), (4, 2)))
        self.assertEqual(paths[1], ((4, 4), (4, 6)))

    def test_duplicate_sample_points_are_skipped(self):
        self.assertEqual(list(dashed_paths(((0, 0), (0, 0), (8, 0)), (3, 2))),
                         list(dashed_paths(((0, 0), (8, 0)), (3, 2))))

    def test_odd_length_pattern_alternates_drawn_and_hidden_segments(self):
        paths = list(dashed_paths(((0, 0), (12, 0)), (2,)))
        self.assertEqual([(p[0][0], p[-1][0]) for p in paths], [(0, 2), (4, 6), (8, 10)])

    def test_invalid_dash_lengths_are_rejected_instead_of_looping(self):
        for pattern in ((), (0, 2), (-1, 2), (float('nan'), 2), (1, float('inf'))):
            with self.assertRaises(ValueError):
                list(dashed_paths(((0, 0), (8, 0)), pattern))


@unittest.skipUnless(ANTIALIAS_AVAILABLE, 'Pillow is not installed')
class RasterTests(unittest.TestCase):
    def test_round_corners_have_partial_pixel_coverage_with_solid_interior(self):
        layer = RasterLayer(48, 32, '#000000')
        white_rectangle(layer)
        picture = layer.render()
        self.assertEqual(picture.size, (48, 32))
        self.assertEqual(picture.getpixel((24, 16)), (255, 255, 255))
        self.assertEqual(picture.getpixel((0, 0)), (0, 0, 0))
        self.assertGreater(sum(0 < pixel[0] < 255 for pixel in pixels(picture)), 30)

    def test_circle_edges_have_partial_coverage(self):
        layer = RasterLayer(32, 32, '#000000')
        layer.add('oval', (5, 5, 27, 27), '#ffffff')
        picture = layer.render()
        self.assertEqual(picture.getpixel((16, 16)), (255, 255, 255))
        self.assertGreater(sum(0 < pixel[0] < 255 for pixel in pixels(picture)), 25)

    def test_diagonal_curve_is_antialiased_without_rounding_its_coordinates(self):
        layer = RasterLayer(64, 40, '#000000')
        original = (3.2, 8.4, 22.7, 12.8, 58.3, 32.1)
        layer.add('line', original, '#ffffff', width=2.8, cap='round')
        picture = layer.render()
        self.assertEqual(layer.commands[0][1], original)
        self.assertGreater(sum(0 < pixel[0] < 255 for pixel in pixels(picture)), 40)

    def test_fractional_node_positions_change_coverage_instead_of_snapping(self):
        first, second = RasterLayer(32, 32, '#000000'), RasterLayer(32, 32, '#000000')
        first.add('oval', (8.1, 8.1, 20.1, 20.1), '#ffffff')
        second.add('oval', (8.8, 8.8, 20.8, 20.8), '#ffffff')
        self.assertNotEqual(first.render().tobytes(), second.render().tobytes())

    def test_outside_nodes_clip_to_the_image_without_wrapping(self):
        layer = RasterLayer(48, 32, '#000000')
        layer.add('oval', (-8, 6, 8, 22), '#ffffff')
        picture = layer.render()
        self.assertEqual(picture.getpixel((0, 14)), (255, 255, 255))
        self.assertEqual(picture.getpixel((47, 14)), (0, 0, 0))

    def test_layer_is_composited_on_its_real_background_without_black_fringe(self):
        layer = RasterLayer(48, 32, '#1b2128')
        layer.add('rounded', (2, 2, 46, 30), '#cdebab', radius=10)
        picture = layer.render()
        self.assertEqual(picture.mode, 'RGB')
        self.assertEqual(tuple(min(pixel[i] for pixel in pixels(picture)) for i in range(3)), (27, 33, 40))

    def test_high_resolution_views_bound_supersampling_memory(self):
        self.assertEqual(RasterLayer(1000, 250, '#000000').scale, 3)
        self.assertEqual(RasterLayer(2500, 800, '#000000').scale, 2)
        self.assertEqual(RasterLayer(0, 0, '#000000').signature[:2], (1, 1))
        for scale in (0, 5, True, 2.5):
            with self.assertRaises(ValueError):
                RasterLayer(48, 32, '#000000', scale)

    def test_invalid_geometry_is_rejected_before_allocating_a_graph(self):
        layer = RasterLayer(48, 32, '#000000')
        for points in ((0, float('nan'), 8, 8), (0, 0, float('inf'), 8), (0, 0, 8)):
            with self.assertRaises(ValueError):
                layer.add('line', points, '#ffffff')
        with self.assertRaises(ValueError):
            layer.add('unknown', (0, 0, 8, 8), '#ffffff')

    def test_graphic_cache_reuses_unchanged_geometry_but_replaces_changed_geometry(self):
        canvas = memory_canvas()
        with patch('smooth_render.ImageTk.PhotoImage') as photo, \
                patch('tkinter.Canvas.delete') as delete, \
                patch('tkinter.Canvas.create_image'), patch('tkinter.Canvas.tag_lower'):
            for _ in range(3):
                with canvas.paint():
                    white_rectangle(canvas._raster)
            self.assertEqual(photo.call_count, 1)
            self.assertEqual(delete.call_count, 3)
            self.assertEqual(delete.call_args.args, ('_smooth_layer',))
            with canvas.paint():
                canvas._raster.add('oval', (8, 8, 20, 20), '#ffffff')
            self.assertEqual(photo.call_count, 2)
        self.assertIsNone(canvas._raster)

    def test_failed_frame_is_not_published_and_cleans_up_drawing_state(self):
        canvas = memory_canvas()
        with patch('smooth_render.ImageTk.PhotoImage') as photo, \
                patch('tkinter.Canvas.create_image') as publish:
            with self.assertRaises(RuntimeError):
                with canvas.paint():
                    white_rectangle(canvas._raster)
                    raise RuntimeError('Interrupted drawing')
            photo.assert_not_called()
            publish.assert_not_called()
        self.assertIsNone(canvas._raster)
        self.assertIsNone(canvas._paint_signature)


class CompatibilityTests(unittest.TestCase):
    def test_destroy_releases_tk_image_before_widget_cleanup_on_calling_thread(self):
        canvas, released = memory_canvas(), []
        class ImageLifetime:
            def __del__(self):
                released.append(threading.get_ident())
        canvas._paint_photo = ImageLifetime()
        with patch('tkinter.Canvas.destroy') as destroy:
            destroy.side_effect = lambda: self.assertEqual(released, [threading.get_ident()])
            canvas.destroy()
            destroy.assert_called_once()
        self.assertIsNone(canvas._paint_photo)

    def test_missing_pillow_keeps_the_standard_tk_drawing_path(self):
        canvas = memory_canvas()
        with patch('smooth_render.ANTIALIAS_AVAILABLE', False), \
                patch('tkinter.Canvas.create_line', return_value=42) as native:
            with canvas.paint():
                self.assertEqual(canvas.create_line(0, 0, 8, 8, fill='#ffffff'), 42)
            native.assert_called_once_with(0, 0, 8, 8, fill='#ffffff')
        self.assertIsNone(canvas._raster)

    def test_decorated_domain_tests_do_not_require_a_tk_window(self):
        owner = Mock()
        owner.canvas = Mock()
        method = painted('canvas')(lambda instance: 'done')
        self.assertEqual(method(owner), 'done')
        owner.canvas.paint.assert_not_called()

    def test_new_widgets_skip_degenerate_round_rect_until_configured(self):
        canvas = memory_canvas()
        canvas._raster = RasterLayer(1, 1, '#1b2128')
        with patch('tkinter.Canvas.create_rectangle', return_value=42):
            self.assertEqual(canvas.create_rounded(1, 1, 0, 0, 12, fill='#ffffff'), 42)
        self.assertEqual(canvas._raster.commands, [])

    def test_color_mix_preserves_endpoints_and_band_hue(self):
        self.assertEqual(blend('#000000', '#ffffff', 0), '#000000')
        self.assertEqual(blend('#000000', '#ffffff', 1), '#ffffff')
        self.assertEqual(blend('#000000', '#ffffff', 0.5), '#808080')
        color = blend('#1b2128', '#b7a0dd', 0.14)
        self.assertGreater(int(color[5:7], 16), int(color[1:3], 16))


if __name__ == '__main__':
    unittest.main()
