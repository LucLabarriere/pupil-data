"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2020 Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""
import logging

import cv2
import numpy as np
from gl_utils import adjust_gl_view, clear_gl_screen, basic_gl_setup
import OpenGL.GL as gl
from glfw import *
from circle_detector import CircleTracker
from platform import system

import audio

from pyglui import ui
from pyglui.cygl.utils import draw_points, draw_polyline, RGBA
from pyglui.pyfontstash import fontstash
from pyglui.ui import get_opensans_font_path
from .base_plugin import CalibrationChoreographyPlugin, GazeDimensionality, ChoreographyMode, ChoreograthyAction, register_calibration_choreography_plugin


logger = logging.getLogger(__name__)


@register_calibration_choreography_plugin("Screen Marker Calibration")
class ScreenMarkerChoreographyPlugin(CalibrationChoreographyPlugin):
    """Calibrate using a marker on your screen
    We use a ring detector that moves across the screen to 9 sites
    Points are collected at sites - not between

    """

    def gazer_for_dimensionality(self, dimensionality: GazeDimensionality):
        if dimensionality == GazeDimensionality.GAZE_3D:
            return int  # FIXME

    @staticmethod
    def __site_locations(dim: GazeDimensionality, mode: ChoreographyMode) -> list:
        if dim == GazeDimensionality.GAZE_3D and mode == ChoreographyMode.CALIBRATION:
            return [
                (0.5, 0.5),
                (0.0, 1.0),
                (1.0, 1.0),
                (1.0, 0.0),
                (0.0, 0.0),
            ]
        if dim == GazeDimensionality.GAZE_3D and mode == ChoreographyMode.ACCURACY_TEST:
            return [
                (0.25, 0.5),
                (0.5, 0.25),
                (0.75, 0.5),
                (0.5, 0.75)
            ]
        if dim == GazeDimensionality.GAZE_2D and mode == ChoreographyMode.CALIBRATION:
            return [
                (0.25, 0.5),
                (0, 0.5),
                (0.0, 1.0),
                (0.5, 1.0),
                (1.0, 1.0),
                (1.0, 0.5),
                (1.0, 0.0),
                (0.5, 0.0),
                (0.0, 0.0),
                (0.75, 0.5),
            ]
        if dim == GazeDimensionality.GAZE_2D and mode == ChoreographyMode.ACCURACY_TEST:
            return [
                (0.5, 0.5),
                (0.25, 0.25),
                (0.25, 0.75),
                (0.75, 0.75),
                (0.75, 0.25),
            ]

    def __init__(
        self,
        g_pool,
        fullscreen=True,
        marker_scale=1.0,
        sample_duration=40,
        monitor_idx=0,
    ):
        super().__init__(g_pool)
        self.screen_marker_state = 0.0
        self.sample_duration = sample_duration  # number of frames to sample per site
        self.lead_in = 25  # frames of marker shown before starting to sample
        self.lead_out = 5  # frames of markers shown after sampling is donw

        self.active_site = None
        self.sites = []
        self.display_pos = -1.0, -1.0
        self.on_position = False
        self.pos = None

        self.marker_scale = marker_scale

        self._window = None

        self.menu = None

        self.monitor_idx = monitor_idx
        self.fullscreen = fullscreen
        self.clicks_to_close = 5

        self.glfont = fontstash.Context()
        self.glfont.add_font("opensans", get_opensans_font_path())
        self.glfont.set_size(32)
        self.glfont.set_color_float((0.2, 0.5, 0.9, 1.0))
        self.glfont.set_align_string(v_align="center")

        # UI Platform tweaks
        if system() == "Linux":
            self.window_position_default = (0, 0)
        elif system() == "Windows":
            self.window_position_default = (8, 90)
        else:
            self.window_position_default = (0, 0)

        self.circle_tracker = CircleTracker()
        self.markers = []

    def get_init_dict(self):
        d = {}
        d["fullscreen"] = self.fullscreen
        d["marker_scale"] = self.marker_scale
        d["monitor_idx"] = self.monitor_idx
        return d

    def init_ui(self):
        super().init_ui()
        self.menu.label = "Screen Marker Calibration"

        def get_monitors_idx_list():
            monitors = [glfwGetMonitorName(m) for m in glfwGetMonitors()]
            return range(len(monitors)), monitors

        if self.monitor_idx not in get_monitors_idx_list()[0]:
            logger.warning(
                "Monitor at index %s no longer availalbe using default"
                % self.monitor_idx
            )
            self.monitor_idx = 0

        self.menu.append(
            ui.Info_Text("Calibrate gaze parameters using a screen based animation.")
        )
        self.menu.append(
            ui.Selector(
                "monitor_idx",
                self,
                selection_getter=get_monitors_idx_list,
                label="Monitor",
            )
        )
        self.menu.append(ui.Switch("fullscreen", self, label="Use fullscreen"))
        self.menu.append(
            ui.Slider(
                "marker_scale", self, step=0.1, min=0.5, max=2.0, label="Marker size"
            )
        )
        self.menu.append(
            ui.Slider(
                "sample_duration",
                self,
                step=1,
                min=10,
                max=100,
                label="Sample duration",
            )
        )

    def deinit_ui(self):
        """gets called when the plugin get terminated.
           either voluntarily or forced.
        """
        if self.is_active:
            self.stop()
        if self._window:
            self.close_window()
        super().deinit_ui()

    def recent_events(self, events):
        frame = events.get("frame")
        if self.is_active and frame:
            gray_img = frame.gray

            if self.clicks_to_close <= 0:
                self.stop()
                return

            # Update the marker
            self.markers = self.circle_tracker.update(gray_img)
            # Screen marker takes only Ref marker
            self.markers = [
                marker for marker in self.markers if marker["marker_type"] == "Ref"
            ]

            if len(self.markers):
                # Set the pos to be the center of the first detected marker
                marker_pos = self.markers[0]["img_pos"]
                self.pos = self.markers[0]["norm_pos"]
            else:
                self.pos = None  # indicate that no reference is detected

            # Check if there are more than one markers
            if len(self.markers) > 1:
                audio.tink()
                logger.warning(
                    "{} markers detected. Please remove all the other markers".format(
                        len(self.markers)
                    )
                )

            # only save a valid ref position if within sample window of calibration routine
            on_position = (
                self.lead_in
                < self.screen_marker_state
                < (self.lead_in + self.sample_duration)
            )

            if on_position and len(self.markers):
                ref = {}
                ref["norm_pos"] = self.pos
                ref["screen_pos"] = marker_pos
                ref["timestamp"] = frame.timestamp
                self.ref_list.append(ref)

            # Always save pupil positions
            self.pupil_list.extend(events["pupil"])

            if on_position and len(self.markers):
                self.screen_marker_state = min(
                    self.sample_duration + self.lead_in, self.screen_marker_state,
                )

            # Animate the screen marker
            if (
                self.screen_marker_state
                < self.sample_duration + self.lead_in + self.lead_out
            ):
                if len(self.markers) or not on_position:
                    self.screen_marker_state += 1
            else:
                self.screen_marker_state = 0
                if not self.sites:
                    self.stop()
                    return
                self.active_site = self.sites.pop(0)
                logger.debug(
                    "Moving screen marker to site at {} {}".format(*self.active_site)
                )

            # use np.arrays for per element wise math
            self.display_pos = np.array(self.active_site)
            self.on_position = on_position
            self.button.status_text = "{}".format(self.active_site)

        if self._window:
            self.gl_display_in_window()

    def gl_display(self):
        """
        use gl calls to render
        at least:
            the published position of the reference
        better:
            show the detected postion even if not published
        """

        # debug mode within world will show green ellipses around detected ellipses
        if self.is_active:
            for marker in self.markers:
                e = marker["ellipses"][-1]  # outermost ellipse
                pts = cv2.ellipse2Poly(
                    (int(e[0][0]), int(e[0][1])),
                    (int(e[1][0] / 2), int(e[1][1] / 2)),
                    int(e[-1]),
                    0,
                    360,
                    15,
                )
                draw_polyline(pts, 1, RGBA(0.0, 1.0, 0.0, 1.0))
                if len(self.markers) > 1:
                    draw_polyline(
                        pts, 1, RGBA(1.0, 0.0, 0.0, 0.5), line_type=gl.GL_POLYGON
                    )

    def start(self):
        if not self.g_pool.capture.online:
            logger.error(f"{self.current_mode_label} requiers world capture video input.")
            return

        super().start()
        audio.say(f"Starting {self.current_mode.label}")
        logger.info(f"Starting {self.current_mode.label}")

        dim = GazeDimensionality.GAZE_3D if self.g_pool.detection_mapping_mode == "3d" else GazeDimensionality.GAZE_2D
        self.sites = self.__site_locations(dim=dim, mode=self.current_mode)
        self.active_site = self.sites.pop(0)

        self.ref_list = []
        self.pupil_list = []
        self.clicks_to_close = 5
        self.open_window(self.current_mode.label)

    def stop(self):
        # TODO: redundancy between all gaze mappers -> might be moved to parent class
        audio.say(f"Stopping {self.current_mode.label}")
        logger.info(f"Stopping {self.current_mode.label}")
        self.smooth_pos = 0, 0
        self.counter = 0
        self.close_window()
        self.button.status_text = ""

        # TODO: This part seems redundant
        if self.current_mode == ChoreographyMode.CALIBRATION:
            self.finish_calibration(self.pupil_list, self.ref_list)
        if self.current_mode == ChoreographyMode.ACCURACY_TEST:
            self.finish_accuracy_test(self.pupil_list, self.ref_list)

        super().stop()

    def open_window(self, title="new_window"):
        if not self._window:
            if self.fullscreen:
                try:
                    monitor = glfwGetMonitors()[self.monitor_idx]
                except Exception:
                    logger.warning(
                        "Monitor at index %s no longer availalbe using default"
                        % self.monitor_idx
                    )
                    self.monitor_idx = 0
                    monitor = glfwGetMonitors()[self.monitor_idx]
                (
                    width,
                    height,
                    redBits,
                    blueBits,
                    greenBits,
                    refreshRate,
                ) = glfwGetVideoMode(monitor)
            else:
                monitor = None
                width, height = 640, 360

            # NOTE: Always creating windowed window here, even if in fullscreen mode. On
            # windows you might experience a black screen for up to 1 sec when creating
            # a blank window directly in fullscreen mode. By creating it windowed and
            # then switching to fullscreen it will stay white the entire time.
            self._window = glfwCreateWindow(
                width, height, title, share=glfwGetCurrentContext()
            )

            if not self.fullscreen:
                glfwSetWindowPos(
                    self._window,
                    self.window_position_default[0],
                    self.window_position_default[1],
                )

            # This makes it harder to accidentally tab out of fullscreen by clicking on
            # some other window (e.g. when having two monitors). On the other hand you
            # want a cursor to adjust the window size when not in fullscreen mode.
            cursor = GLFW_CURSOR_DISABLED if self.fullscreen else GLFW_CURSOR_HIDDEN

            glfwSetInputMode(self._window, GLFW_CURSOR, cursor)

            # Register callbacks
            glfwSetFramebufferSizeCallback(self._window, on_resize)
            glfwSetKeyCallback(self._window, self.on_window_key)
            glfwSetMouseButtonCallback(self._window, self.on_window_mouse_button)
            on_resize(self._window, *glfwGetFramebufferSize(self._window))

            # gl_state settings
            active_window = glfwGetCurrentContext()
            glfwMakeContextCurrent(self._window)
            basic_gl_setup()
            # refresh speed settings
            glfwSwapInterval(0)

            glfwMakeContextCurrent(active_window)

            if self.fullscreen:
                # Switch to full screen here. See NOTE above at glfwCreateWindow().
                glfwSetWindowMonitor(
                    self._window, monitor, 0, 0, width, height, refreshRate
                )

    def close_window(self):
        if self._window:
            # enable mouse display
            active_window = glfwGetCurrentContext()
            glfwSetInputMode(self._window, GLFW_CURSOR, GLFW_CURSOR_NORMAL)
            glfwDestroyWindow(self._window)
            self._window = None
            glfwMakeContextCurrent(active_window)

    def gl_display_in_window(self):
        if glfwWindowShouldClose(self._window):
            self.stop()
            return

        p_window_size = glfwGetFramebufferSize(self._window)
        if p_window_size == (0, 0):
            # On Windows we get a window_size of (0, 0) when either minimizing the
            # Window or when tabbing out (rendered only in the background). We get
            # errors when we call the code below with window size (0, 0). Anyways we
            # probably want to stop calibration in this case as it will screw up the
            # calibration anyways.
            self.stop()
            return

        active_window = glfwGetCurrentContext()
        glfwMakeContextCurrent(self._window)

        clear_gl_screen()

        hdpi_factor = getHDPIFactor(self._window)
        r = self.marker_scale * hdpi_factor
        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        gl.glOrtho(0, p_window_size[0], p_window_size[1], 0, -1, 1)
        # Switch back to Model View Matrix
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadIdentity()

        def map_value(value, in_range=(0, 1), out_range=(0, 1)):
            ratio = (out_range[1] - out_range[0]) / (in_range[1] - in_range[0])
            return (value - in_range[0]) * ratio + out_range[0]

        pad = 90 * r
        screen_pos = (
            map_value(self.display_pos[0], out_range=(pad, p_window_size[0] - pad)),
            map_value(self.display_pos[1], out_range=(p_window_size[1] - pad, pad)),
        )
        alpha = interp_fn(
            self.screen_marker_state,
            0.0,
            1.0,
            float(self.sample_duration + self.lead_in + self.lead_out),
            float(self.lead_in),
            float(self.sample_duration + self.lead_in),
        )

        r2 = 2 * r
        draw_points(
            [screen_pos], size=60 * r2, color=RGBA(0.0, 0.0, 0.0, alpha), sharpness=0.9
        )
        draw_points(
            [screen_pos], size=38 * r2, color=RGBA(1.0, 1.0, 1.0, alpha), sharpness=0.8
        )
        draw_points(
            [screen_pos], size=19 * r2, color=RGBA(0.0, 0.0, 0.0, alpha), sharpness=0.55
        )

        # some feedback on the detection state
        color = (
            RGBA(0.0, 0.8, 0.0, alpha)
            if len(self.markers) and self.on_position
            else RGBA(0.8, 0.0, 0.0, alpha)
        )
        draw_points([screen_pos], size=3 * r2, color=color, sharpness=0.5)

        if self.clicks_to_close < 5:
            self.glfont.set_size(int(p_window_size[0] / 30.0))
            self.glfont.draw_text(
                p_window_size[0] / 2.0,
                p_window_size[1] / 4.0,
                f"Touch {self.clicks_to_close} more times to cancel {self.current_mode.label}.",
            )

        glfwSwapBuffers(self._window)
        glfwMakeContextCurrent(active_window)

    def on_window_key(self, window, key, scancode, action, mods):
        if action == GLFW_PRESS:
            if key == GLFW_KEY_ESCAPE:
                self.clicks_to_close = 0

    def on_window_mouse_button(self, window, button, action, mods):
        if action == GLFW_PRESS:
            self.clicks_to_close -= 1


# window calbacks
def on_resize(window, w, h):
    active_window = glfwGetCurrentContext()
    glfwMakeContextCurrent(window)
    adjust_gl_view(w, h)
    glfwMakeContextCurrent(active_window)


# easing functions for animation of the marker fade in/out
def easeInOutQuad(t, b, c, d):
    """Robert Penner easing function examples at: http://gizma.com/easing/
    t = current time in frames or whatever unit
    b = beginning/start value
    c = change in value
    d = duration

    """
    t /= d / 2
    if t < 1:
        return c / 2 * t * t + b
    t -= 1
    return -c / 2 * (t * (t - 2) - 1) + b


def interp_fn(t, b, c, d, start_sample=15.0, stop_sample=55.0):
    # ease in, sample, ease out
    if t < start_sample:
        return easeInOutQuad(t, b, c, start_sample)
    elif t > stop_sample:
        return 1 - easeInOutQuad(t - stop_sample, b, c, d - stop_sample)
    else:
        return 1.0
