import numpy as np
from time import time

class Pillar:
  ignore = False
  big_correction = False
  def __init__(self, screen_x: int, width: int, height: int, color: str):
    self.screen_x = screen_x
    self.width = width
    self.height = height
    self.color = color


def extract_ROI(image: np.ndarray, startxy: list, endxy: list) -> np.ndarray:
  """
  Extracts the ROIs from the image
  """
  
  return image[startxy[1]:endxy[1], startxy[0]:endxy[0]]

def print_past_time(message: str):
  """
  Prints the message with the current time
  """
  global last_time
  if 'last_time' not in globals():
    last_time = time()
    print(f"last_time not defined, setting to {last_time}")
  current_time = time()
  print(f"{(current_time - last_time):.3f}: {message}")
  last_time = current_time