import numpy as np
from time import time

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
                  (x1, y1, x2, y2), slope (m), y-intercept (b), and length of a line.
  """

  def __init__(self, lines: np.ndarray, xy_offset: tuple = (0, 0)):
    """
    Initialize the Lines object.

    Args:
      lines (np.ndarray): The array of line coordinates, where each line is represented
                          as [[x1, y1, x2, y2]].
      xy_offset (tuple, optional): A tuple (x_offset, y_offset) for the purpose of drawing the lines
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
          "x1": line[0][0],
          "y1": line[0][1],
          "x2": line[0][2],
          "y2": line[0][3],
          "x_offset": x_offset,
          "y_offset": y_offset
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

  def print(self):
    """
    Print the details of the straight section.
    """
    print(f"Straight section {self.index}:")
    print(f"    l,    r")
    for i in range(2, -1, -1):
      print(f"{i}: {self.l[i]}, {self.r[i]}")
    print(f"driving_pos: {self.driving_pos}")

  def calculate_driving_pos(self) -> list:
    """
    Calculate the driving position of the robot in the straight section.

    Returns:
        list: The driving position as [x, y].
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
