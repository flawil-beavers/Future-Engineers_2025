from time import time as _time

def time():
  SPEED = 80
  return _time() * (SPEED / 80.0)