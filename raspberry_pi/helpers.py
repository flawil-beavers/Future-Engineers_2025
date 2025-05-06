import numpy as np
from time import time

class Pillar:
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
      y (int): The y-coordinate of base of the pillar.
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
  Extracts the ROIs from the image.
  """
  return image[startxy[1]:endxy[1], startxy[0]:endxy[0]]

def print_past_time(message: str):
  """
  Prints the message with the current time.
  """
  global last_time
  if 'last_time' not in globals():
    last_time = time()
    print(f"last_time not defined, setting to {last_time}")
  current_time = time()
  print(f"{(current_time - last_time):.3f}: {message}")
  last_time = current_time
  
class Straight_Section:
  """
  Class to represent a straight section of the field.
  """
  def __init__(self, index: int):
    self.index = index
    self.r = [0, 0, 0]
    self.l = [0, 0, 0]
    self.parking_lot = False
    self.driving_pos = [0, 0]

  def print(self):
    print(f"Straight section {self.index}:")
    print(f"    l,    r")
    for i in range(2, -1, -1):
      print(f"{i}: {self.l[i]}, {self.r[i]}")
    print(f"driving_pos: {self.driving_pos}")
      
  def calculate_driving_pos(self) -> list: 
    """
    Calculate the driving position of the robot in the straight section.
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
  