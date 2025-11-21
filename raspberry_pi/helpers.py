import numpy as np
from time import time, sleep, perf_counter
import sys
from collections import deque

class Pillar:
    """
    Represents a pillar detected in the image.

    This class stores both image-space measurements and optional world-
    space coordinates if available.

    Attributes:
        screen_x (int): Horizontal pixel coordinate of the pillar in the image.
        width (int): Pixel width of the detected pillar.
        height (int): Pixel height of the detected pillar.
        color (str): Classified color label ("RED", "GREEN").
        x (int): Real-world x-coordinate of the pillar (default 0).
        y (int): Real-world y-coordinate of the pillar base (default 0).
        ignore (bool): If True, downstream logic should ignore this pillar.
    """

    ignore = False

    def __init__(self, screen_x: int, width: int, height: int, color: str, x: int = 0, y: int = 0):
        """
        Initialize a Pillar instance.

        Args:
            screen_x (int): X-coordinate in the image.
            width (int): Pixel width of the pillar.
            height (int): Pixel height of the pillar.
            color (str): Detected color ("RED", "GREEN").
            x (int, optional): Real-world x-coordinate (default 0).
            y (int, optional): Real-world y-coordinate at base (default 0).
        """
        self.screen_x = screen_x
        self.width = width
        self.height = height
        self.color = color
        self.x = x
        self.y = y


def bound(value: float, min_value: float = -1, max_value: float = 1) -> float:
    """
    Clamp a value between min_value and max_value.

    Args:
        value (float): Input value.
        min_value (float): Lower bound.
        max_value (float): Upper bound.

    Returns:
        float: Clamped value.
    """
    return max(min(value, max_value), min_value)


def extract_ROI(image: np.ndarray, startxy: list, endxy: list) -> np.ndarray:
    """
    Extract a rectangular region from the image.

    Args:
        image (np.ndarray): Source image.
        startxy (list[int, int]): Top-left pixel coordinate [x, y].
        endxy (list[int, int]): Bottom-right pixel coordinate [x, y].

    Returns:
        np.ndarray: Extracted region.
    """
    return image[startxy[1]:endxy[1], startxy[0]:endxy[0]]


def print_past_time(message: str):
    """
    Deprecated: Use LoopTimerRegistry instead.
    
    Print a diagnostic time delta between successive calls.

    Useful for lightweight profiling. On first call, a reference timestamp is
    created automatically.

    Args:
        message (str): Message to display with elapsed time.
    """
    global last_time
    if 'last_time' not in globals():
        last_time = time()
        print(f"last_time not defined, setting to {last_time}")
    current_time = time()
    print(f"{(current_time - last_time):.3f}: {message}")
    last_time = current_time


class Lines:
    """
    Represents detected lines from image processing.

    Each stored line dictionary contains:
        x1, y1, x2, y2 : Endpoints (after calc_offset)
        x              : Midpoint x-coordinate
        x_offset, y_offset : Drawing offsets
        m, b           : Slope and intercept (inf for vertical)
        length         : Euclidean length
    """

    def __init__(self, lines: np.ndarray, xy_offset: tuple = (0, 0), calc_offset: tuple = (0, 0)):
        """
        Initialize Lines

        Args:
            lines (np.ndarray or None): Array of detected lines in format
                [[x1, y1, x2, y2]]. If None or empty, creates an empty object.
            xy_offset (tuple[int, int]): Pixel offsets used when drawing lines.
            calc_offset (tuple[int, int]): Pixel offsets applied before slope/
                intercept and length calculations.
        """
        self.lines = []
        x_offset, y_offset = xy_offset

        if lines is None or len(lines) == 0:
            return

        for line in lines:
            line_data = {
                "x1": line[0][0] - calc_offset[0],
                "y1": line[0][1] - calc_offset[1],
                "x2": line[0][2] - calc_offset[0],
                "y2": line[0][3] - calc_offset[1],
                "x": (line[0][0] + line[0][2]) / 2 - calc_offset[0],
                "x_offset": x_offset + calc_offset[0],
                "y_offset": y_offset + calc_offset[1]
            }
            self.lines.append(line_data)

        self.compute_slope_form()
        self.sort_length()

    def compute_slope_form(self):
        """
        Compute slope, intercept, and length for each stored line.

        - Vertical: m = inf, b = inf
        - Horizontal: m = 0, b = constant y
        - Otherwise: standard slope and intercept formulas
        """
        for line in self.lines:
            if line['y1'] == line['y2']:
                line['m'] = 0
                line['b'] = line['y1']
            elif line['x1'] == line['x2']:
                line['m'] = float('inf')
                line['b'] = float('inf')
            else:
                line['m'] = (line['y2'] - line['y1']) / (line['x2'] - line['x1'])
                line['b'] = line['y1'] - line['m'] * line['x1']

            line['length'] = ((line['x2'] - line['x1'])**2 +
                              (line['y2'] - line['y1'])**2)**0.5

    def sort_length(self):
        """
        Sort lines in descending order of length.

        Longest line will appear first in self.lines.
        """
        self.lines.sort(key=lambda x: x['length'], reverse=True)


class Straight_Section:
    """
    Represents a straight track segment containing pillar detections and parking info.

    Attributes:
        index (int): Section index.
        r (list[int]): Three right-side pillars (0 if none).
        l (list[int]): Three left-side pillars (0 if none).
        parking_lot (bool): True if this section contains a parking lot.
        driving_pos (list[int, int]): Computed driving position for the car.
    """

    def __init__(self, index: int):
        """
        Initialize a Straight_Section.

        Args:
            index (int): Section index.
        """
        self.index = index
        self.r = [0, 0, 0]
        self.l = [0, 0, 0]
        self.parking_lot = False
        self.driving_pos = [0, 0]

    def validate(self, direction: int = 1):
        """
        Fix small errors related to pillar assignment in parking-lot sections.

        If the section contains a parking lot, all pillars are moved to one side.

        Args:
            direction (int): +1 for clockwise, -1 for counter-clockwise.
        """
        if self.parking_lot:
            for i in range(3):
                if self.l[i] != 0 and direction == -1:
                    self.r[i] = self.l[i]
                    print(f"Fixing parking lot pillar: l[{i}] = {self.l[i]} -> r[{i}] = {self.r[i]}")
                elif self.r[i] != 0 and direction == 1:
                    self.l[i] = self.r[i]
                    print(f"Fixing parking lot pillar: l[{i}] = {self.l[i]} <- r[{i}] = {self.r[i]}")

    def print(self):
        """
        Print detailed information about the straight section.
        """
        print(f"Straight section {self.index}:")
        print("    l,    r")
        for i in range(2, -1, -1):
            print(f"{i}: {self.l[i]}, {self.r[i]}")
        print(f"parking_lot: {self.parking_lot}")
        print(f"driving_pos: {self.driving_pos}")

    def calculate_driving_pos(self) -> list:
        """
        Compute driving position based on detected pillars.

        If two valid different pillars exist at front and back (0 and 2), they
        determine the driving position. Otherwise the first found pillar is used.

        Returns:
            list[int, int]: Driving position [front, back].
        """
        pillars = [0, 0, 0]
        for i in range(3):
            if self.l[i] != 0:
                pillars[i] = self.l[i]
            elif self.r[i] != 0:
                pillars[i] = self.r[i]

        if pillars[0] != 0 and pillars[2] != 0 and pillars[0] != pillars[2]:
            self.driving_pos = [pillars[0], pillars[2]]
        else:
            for i in pillars:
                if i != 0:
                    self.driving_pos = [i, i]
                    break
        return self.driving_pos


def setup_logging():
    """
    Redirect stdout and stderr to 'logs/robot_log.txt'.

    Creates the 'logs' directory if necessary and logs everything written via
    print() and uncaught exceptions.
    """
    log_file = open("logs/robot_log.txt", "a")

    class Logger:
        def __init__(self, file):
            self.file = file

        def write(self, message):
            self.file.write(message)
            self.file.flush()

        def flush(self):
            pass

    sys.stdout = Logger(log_file)
    sys.stderr = Logger(log_file)


class Car:
    """
    Represents the car's current state including motion, steering and flags.

    Attributes:
        angle (float): Current yaw angle in degrees.
        distance (float): Encoder distance in millimeters.
        speed (float): Current speed value.
        steering (float): Current steering command.
        straight_direction (int): Current straight-section direction.
        drift_rate_last_sec (float): Drift rate estimate for the last second.
        drift_rate_time (float): Timestamp of last drift update.
        paused (bool): Whether car movement is paused.
        stalled (bool): Whether the car has stalled.
    """

    def __init__(self, angle=0.0, distance=0.0, speed=0.0, steering=0.0):
        self.angle = angle
        self.distance = distance
        self.speed = speed
        self.steering = steering

        self.straight_direction = 0
        self.drift_rate_last_sec = 0
        self.drift_rate_time = 0
        self.paused = True
        self.stalled = False


class SharedState:
    """
    Centralized container for all shared system state.

    Includes:
        - Camera stream states
        - Line and pillar detections
        - Position and round tracking
        - Parking information
        - Control flags (pillars mode, calibration, shutdown)
        - PD control parameters
        - Error metrics for steering adjustments
    """

    def __init__(self):
        # Streams
        self.current_streams = ["viz", "pink", "viz"]
        self.has_sent_streams_info = False
        self.active_websocket = None
        self.latest_streams = {}
        self.detected_corners = {"L": None, "R": None, "P": []}

        # Vision results
        self.border_lines = {"L": None, "R": None, "M": None}
        self.detected_pillars = []
        self.portion_black_l = 0.0
        self.portion_black_r = 0.0
        self.portion_orange = 0.0
        self.portion_blue = 0.0
        self.distance_front = 0.0

        self.round_dir = 0
        self.rounds = 0
        self.position = "middle"

        self.current_function = "starting"
        self.parking = None
        self.parking_x = 0
        self.parking_y = 0
        self.vertical_line = None
        self.lower_point = 0
        self.distance_pink = 0

        # Flags
        self.headless = False
        self.pillars = False
        self.shutdown = False
        self.calibrate = False
        self.skip_arduino = False
        self.hq = False

        # Error tracking
        self.last_error = 0.0
        # Timestamp of the last error sample (perf_counter seconds). Used by PD controller.
        self.last_error_time = None

        # PD parameters
        self.kp = 0.0
        self.kd = 0.0
        # If set, contains list of ROI labels to process (e.g. ['L'] or ['R']).
        # When None, the detector should process both sides.
        self.active_roi_sides = None

    def reset_streams(self):
        """Clear all stored latest stream data."""
        self.latest_streams.clear()

    def update_stream(self, name, value):
        """Update a specific stream entry."""
        self.latest_streams[name] = value

    def set_lines(self, left, right):
        """Set detected border line objects."""
        self.border_lines["L"] = left
        self.border_lines["R"] = right

    def add_pillars(self, pillars):
        """Replace detected pillars list."""
        self.detected_pillars = pillars

    def set_flags(self, headless=False, pillars=False, shutdown=False, calibrate=False, skip_arduino=False, hq=False):
        """Update control and mode flags."""
        self.headless = headless
        self.pillars = pillars
        self.shutdown = shutdown
        self.calibrate = calibrate
        self.skip_arduino = skip_arduino
        self.hq = hq


def find_direction(round_direction, current_position, target_position):
    """
    Determine the direction (+1 or -1) needed to transition between lane positions.

    Treats "middle_parking" as "middle" for compatibility.

    Args:
        round_direction (int): +1 or -1 depending on track direction.
        current_position (str): One of "inner", "middle", "outer", "middle_parking".
        target_position (str): One of "inner", "middle", "outer", "middle_parking".

    Returns:
        int: Steering direction (-1, 0, +1).

    Raises:
        ValueError: If inputs are invalid.
    """
    if current_position == "middle_parking":
        current_position = "middle"
    if target_position == "middle_parking":
        target_position = "middle"

    if current_position == target_position:
        return 0
    elif current_position == "inner":
        direction = -1
    elif current_position == "outer":
        direction = 1
    elif target_position == "inner":
        direction = 1
    elif target_position == "outer":
        direction = -1
    else:
        raise ValueError(f"Invalid position: {current_position} or {target_position}")

    return -direction * round_direction


class AngleBuffer:
    """
    Maintains a rolling buffer of (timestamp, angle) samples over a time window.

    Provides smoothed statistics including:
        - mean angle
        - MSE (mean squared error)
    """

    def __init__(self, window_seconds: float = 1.0):
        self.window = window_seconds
        self.buf = deque()

    def append(self, timestamp: float, angle: float):
        """
        Add a sample to the buffer and discard outdated entries.

        Args:
            timestamp (float): Timestamp of sample.
            angle (float): Angle measurement.
        """
        self.buf.append((timestamp, angle))
        self._trim(timestamp)

    def _trim(self, now: float):
        """Remove samples older than window_seconds."""
        while self.buf and (now - self.buf[0][0]) > self.window:
            self.buf.popleft()

    def mean_and_mse(self):
        """
        Compute mean and MSE of stored angles.

        Returns:
            tuple (mean: float or None, mse: float or None)
        """
        if not self.buf:
            return None, None
        angles = [a for _, a in self.buf]
        mean = sum(angles) / len(angles)
        mse = sum((a - mean) ** 2 for a in angles) / len(angles)
        return mean, mse

    def clear(self):
        """Remove all stored samples."""
        self.buf.clear()

    def covers_full_window(self, now: float = None) -> bool:
        """
        Check if buffer spans at least window_seconds.

        Args:
            now (float, optional): Timestamp reference. Defaults to current time.

        Returns:
            bool: True if time coverage is adequate.
        """
        if now is None:
            now = time()
        if not self.buf:
            return False
        return (now - self.buf[0][0]) >= self.window


class LoopTimerRegistry:
    """
    Tracks how long loops take between calls.

    Designed so each loop calls record('loop_name') once per iteration.
    """

    def __init__(self):
        self._last_call = {}
        self._last_duration = {}

    def record(self, name: str):
        """
        Record a loop iteration timestamp and compute the duration since last entry.

        Args:
            name (str): Loop identifier.
        """
        now = perf_counter()
        if name in self._last_call:
            self._last_duration[name] = now - self._last_call[name]
        self._last_call[name] = now

    def get_last_durations(self):
        """
        Retrieve recorded loop durations.

        Returns:
            dict: Map of loop_name -> last iteration duration.
        """
        return dict(self._last_duration)
