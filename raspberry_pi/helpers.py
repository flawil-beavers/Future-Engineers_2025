import numpy as np
from time import time, sleep
import sys
import os
import serial
import cv2

class Pillar:
    """
    Class to represent a pillar detected in the image.

    Attributes:
        screen_x (int): The x-coordinate of the pillar on the screen.
        width (int): The width of the pillar.
        height (int): The height of the pillar.
        color (str): The color of the pillar (e.g., "RED", "BLUE").
        x (int): The x-coordinate of the pillar in the real world (default: 0).
        y (int): The y-coordinate of the base of the pillar in the real world (default: 0).
        ignore (bool): Whether the pillar should be ignored (default: False).
        big_correction (bool): Whether the pillar requires a big correction (default: False).
    """

    ignore = False
    big_correction = False

    def __init__(self, screen_x: int, width: int, height: int, color: str, x: int = 0, y: int = 0):
        """
        Initialize a Pillar object.

        Args:
            screen_x (int): The x-coordinate on the screen.
            width (int): The width of the pillar.
            height (int): The height of the pillar.
            color (str): The color of the pillar.
            y (int): The y-coordinate of the base of the pillar in the real world (default: 0).
        """
        self.screen_x = screen_x
        self.width = width
        self.height = height
        self.color = color
        self.y = y


def bound(value: float, min_value: float = -1, max_value: float = 1) -> float:
    """
    Bound the value to be within the min and max values.

    Args:
        value (float): The value to be bounded.
        min_value (float, optional): The minimum value. Defaults to -1.
        max_value (float, optional): The maximum value. Defaults to 1.

    Returns:
        float: The bounded value.
    """
    return max(min(value, max_value), min_value)


def extract_ROI(image: np.ndarray, startxy: list, endxy: list) -> np.ndarray:
    """
    Extract a region of interest (ROI) from the image.

    Args:
        image (np.ndarray): The input image.
        startxy (list): The starting (x, y) coordinates of the ROI.
        endxy (list): The ending (x, y) coordinates of the ROI.

    Returns:
        np.ndarray: The extracted ROI.
    """
    return image[startxy[1]:endxy[1], startxy[0]:endxy[0]]


def print_past_time(message: str):
    """
    Print a message with the elapsed time since the last call.

    Args:
        message (str): The message to print.
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
    Class to represent the detected lines in the image.

    Attributes:
        lines (list): A list of dictionaries, where each dictionary contains the coordinates
                      (x1, y1, x2, y2) of a line, its offset (x_offset, y_offset),
                      slope (m), y-intercept (b), and length.
    """

    def __init__(self, lines: np.ndarray, xy_offset: tuple = (0, 0), calc_offset: tuple = (0, 0)):
        """
        Initialize the Lines object.

        Args:
            lines (np.ndarray): The array of line coordinates, where each line is represented
                                as [[x1, y1, x2, y2]].
            xy_offset (tuple, optional): A tuple (x_offset, y_offset) for the purpose of drawing the lines
            calc_offset (tuple, optional): A tuple (x_offset, y_offset) for the purpose of calculating the slope and intercept.
        """
        self.lines = []  # List to store processed line data
        x_offset, y_offset = xy_offset
        # check if lines is a list of lines and not None
        if lines is None or len(lines) == 0:
            return
        else:
            for line in lines:
                # Extract line coordinates and apply the offset
                line_data = {
                    "x1": line[0][0] - calc_offset[0],
                    "y1": line[0][1] - calc_offset[1],
                    "x2": line[0][2] - calc_offset[0],
                    "y2": line[0][3] - calc_offset[1],
                    "x_offset": x_offset + calc_offset[0],
                    "y_offset": y_offset + calc_offset[1]
                }
                self.lines.append(line_data)
        self.compute_slope_form()  # Compute slope and intercept for each line
        self.sort_length()         # Sort lines by their lengths

    def compute_slope_form(self):
        """
        Compute the slope-intercept form (y = mx + b) and length of each line.

        Adds the following keys to each line dictionary:
            - 'm': Slope of the line (float). If the line is vertical, slope is set to infinity.
            - 'b': Y-intercept of the line (float). If the line is vertical, intercept is set to infinity.
            - 'length': Length of the line (float), calculated using the Euclidean distance formula.
        """
        for line in self.lines:
            if line['y1'] == line['y2']:
                # Horizontal line: slope is 0, intercept is the y-coordinate
                line['m'] = 0
                line['b'] = line['y1']
            elif line['x1'] == line['x2']:
                # Vertical line: slope and intercept are set to infinity
                line['m'] = float('inf')
                line['b'] = float('inf')
            else:
                # General case: calculate slope and intercept
                line['m'] = (line['y2'] - line['y1']) / (line['x2'] - line['x1'])
                line['b'] = line['y1'] - line['m'] * line['x1']
            # Calculate the length of the line using the Euclidean distance formula
            line['length'] = ((line['x2'] - line['x1'])**2 + (line['y2'] - line['y1'])**2)**0.5

    def sort_length(self):
        """
        Sort the lines based on their lengths in descending order.

        The longest line will be the first element in the `self.lines` list.
        """
        self.lines.sort(key=lambda x: x['length'], reverse=True)


class Straight_Section:
    """
    Class to represent a straight section of the field.

    Attributes:
        index (int): The index of the straight section.
        r (list): The right pillars in the section.
        l (list): The left pillars in the section.
        parking_lot (bool): Whether the section contains a parking lot.
        driving_pos (list): The driving position in the section.
    """

    def __init__(self, index: int):
        """
        Initialize the Straight_Section object.

        Args:
            index (int): The index of the straight section.
        """
        self.index = index
        self.r = [0, 0, 0]
        self.l = [0, 0, 0]
        self.parking_lot = False
        self.driving_pos = [0, 0]

    def validate(self, direction: int = 1):
        """
        Validate the straight section and fixes small errors related to the parking lot.
        
        Args:
            direction (int): The direction of the robot. 1 for clockwise, -1 for counter-clockwise.
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
        Print the details of the straight section.
        """
        print(f"Straight section {self.index}:")
        print(f"    l,    r")
        for i in range(2, -1, -1):
            print(f"{i}: {self.l[i]}, {self.r[i]}")
        print(f"parking_lot: {self.parking_lot}")
        print(f"driving_pos: {self.driving_pos}")

    def calculate_driving_pos(self) -> list:
        """
        Calculate the driving position of the robot in the straight section.

        Returns:
            list: The driving position as [fist color pillar, second color pillar].
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
    Set up logging to a file and redirect print statements to the log file.
    This function creates a log file named 'robot_log.txt' in the 'logs' directory.
    If the directory does not exist, it will be created.
    """
    
    # Open a file for logging
    log_file = open("logs/robot_log.txt", "a")

    # Redirect print statements to the log file
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
    

def process_pillars(state, straight_sections):
    """
    Process detected pillars and update the straight sections with pillar information.
    Args:
        state (SharedState): The shared state containing detected pillars and other information.
        straight_sections (list): A list of Straight_Section objects representing the straight sections.
    """
    if state.rounds == 0:
        first_section = True
    else:
        first_section = False
    section_index = state.rounds % 4
    if state.rounds > 4:
        pillar_driving_pos = straight_sections[section_index].calculate_driving_pos() # todo: in last example red was not saved although it was detected
        return pillar_driving_pos
    index = None
    if straight_sections[section_index].parking_lot:
        print(f"parking lot in section, resetting pillars")
        # rescan the parking lot
        for i in range(3):
            straight_sections[section_index].l[i] = 0
            straight_sections[section_index].r[i] = 0
        # print(f"pillars reset and printing now")
        # straight_sections[section_index].print()
    for p in state.detected_pillars: # todo show where the undetected pillars are
        index = None
        if p.ignore:
            cv2.rectangle(state.latest_streams["viz"], (p.screen_x - int(p.width*0.35), p.y-p.height), (p.screen_x + int(p.width*0.35), p.y), ((0, 0, 50) if p.color == "RED" else (0, 50, 0)), 3)
            cv2.putText(state.latest_streams["viz"], f"{p.color} {int(p.y)} {index}", (p.screen_x - int(p.width*0.35), p.y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            continue
        if first_section:
            if p.y > 130:
                print(f"--Pillar {p.color} is too near (higher than 130), y={p.y}")
            if abs(p.screen_x - 320) > 200:
                print(f"--Pillar {p.color} is too far from the center (further than 200), x={abs(p.screen_x - 320)}")
            if p.y > 50:
                index = 0
            elif p.y > 28:
                index = 1
            else:
                print(f"--Pillar {p.color} is too far (lower than 29), y={p.y}")
        else:
            if p.y > 180:
                print(f"--Pillar {p.color} is too near (higher than 180), y={p.y}")
            if p.y > 50:
                index = 0 # about 80
            elif p.y > 22:
                index = 1 # about 28
            elif p.y > 11:
                index = 2 # about 13
            else:
                print(f"--Pillar {p.color} is too far (lower than 25), y={p.y}")
        if index == None:
            cv2.rectangle(state.latest_streams["viz"], (p.screen_x - int(p.width*0.35), p.y-p.height), (p.screen_x + int(p.width*0.35), p.y), ((0, 0, 100) if p.color == "RED" else (0, 100, 0)), 3)
            cv2.putText(state.latest_streams["viz"], f"{p.color} {int(p.y)} {index}", (p.screen_x - int(p.width*0.35), p.y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            continue
        else:
            cv2.rectangle(state.latest_streams["viz"], (p.screen_x - int(p.width*0.35), p.y-p.height), (p.screen_x + int(p.width*0.35), p.y), ((0, 0, 255) if p.color == "RED" else (0, 255, 0)), 3)
            cv2.putText(state.latest_streams["viz"], f"{p.color} {int(p.y)} {index}", (p.screen_x - int(p.width*0.35), p.y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            print(f"Pillar {p.color} accepted at index {index}, y={p.y}, x={p.screen_x}, section {section_index}")
        if p.screen_x < 320:
            straight_sections[section_index].l[index] = p.color
        else:
            straight_sections[section_index].r[index] = p.color

    straight_sections[section_index].parking_lot = True if section_index == 0 else False
    straight_sections[section_index].validate(state.round_dir)
    cv2.imwrite(f"logs/image{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["color_image"])
    cv2.imwrite(f"logs/image_viz{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["viz"])
    pillar_driving_pos = straight_sections[section_index].calculate_driving_pos() # todo: in last example red was not saved although it was detected
    straight_sections[section_index].print()
    return pillar_driving_pos

    
class Car:
    """
    Class to represent the car's current state.

    Attributes:
        angle (float): The current angle of the car (degrees).
        distance (float): The current encoder distance (millimeters).
        speed (float): The current speed of the car.
        steering (float): The current steering value.
    """

    def __init__(self, angle=0.0, distance=0.0, speed=0.0, steering=0.0):
        self.angle = angle
        self.distance = distance
        self.speed = speed
        self.steering = steering
        
        # paused
        self.paused = True
        self.stalled = False


class SharedState:
    """
    Central place to store and access all shared data:
    - Streams (camera images, ROIs, HSV, etc.)
    - Lines (detected wall/edge lines)
    - Pillars (red/green markers)
    - State flags (pillars mode, calibration, shutdown, etc.)
    """
    def __init__(self):
        # Streams
        self.current_streams = ["viz", "black", "viz"]
        self.has_sent_streams_info = False
        self.active_websocket = None
        self.latest_streams = {}
        self.detected_corners = {"L": None, "R": None} # corner average x, corner average y, index of line, same or different slopes

        # Vision results
        self.border_lines = {"L": None, "R": None}
        self.detected_pillars = []
        self.portion_black_l = 0.0
        self.portion_black_r = 0.0
        self.portion_orange = 0.0
        self.portion_blue = 0.0
        self.distance_front = 0.0
        
        self.round_dir = 0
        self.rounds = 0
        self.position = "middle"
        
        self.straight_direction = 0
        
        self.current_function = "starting"

        # Configurable flags
        self.headless = False
        self.pillars = False
        self.shutdown = False
        self.calibrate = False
        self.skip_arduino = False

        # Error tracking
        self.last_error = 0.0

        # PD control params
        self.kp = 0.0
        self.kd = 0.0

    def reset_streams(self):
        self.latest_streams.clear()

    def update_stream(self, name, value):
        self.latest_streams[name] = value

    def set_lines(self, left, right):
        self.border_lines["L"] = left
        self.border_lines["R"] = right

    def add_pillars(self, pillars):
        self.detected_pillars = pillars

    def set_flags(self, headless=False, pillars=False, shutdown=False, calibrate=False, skip_arduino=False):
        self.headless = headless
        self.pillars = pillars
        self.shutdown = shutdown
        self.calibrate = calibrate
        self.skip_arduino = skip_arduino

def find_direction(round_direction, current_position, target_position):
    # change middle_parking to middle, if current_position or target_position is middle_parking
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
    # current_position == "middle"
    elif target_position == "inner":
        direction = 1
    elif target_position == "outer":
        direction = -1
    else:
        raise ValueError(f"Invalid position: {current_position} or {target_position}")
    return -direction * round_direction