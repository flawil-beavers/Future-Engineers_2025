from fftime import time
from helpers import Pillar

class StateMachine:

  current_state = "STARTING"
  round_dir = 0
  turns_left = 12

  last_state_distance = 0.0  # Distance at the last state transition
  total_distance = 0.0  # Total distance traveled
  diff_distance = 0.0  # Difference in distance for state transition
  
  last_state_angle = 0.0  # Angle at the last state transition
  total_angle = 0.0  # Total angle turned
  diff_angle = 0.0  # Difference in angle for state transition
  
  search_for_dir = True

  _scheduled_state = None
  _scheduled_state_distance = None  # Distance for scheduled state transition
  _scheduled_state_angle = None  # Angle for scheduled state transition
  _allow_pillar_detection = True

  take_picture = False
  distance_take_picture = 0.0
  _took_picture = False

  next_pillar = None
  first_line_found = None

  avoid_big = False

  def __init__(self, isPillarRound: bool = False) -> None:
    self.last_state_distance = 0.0
    self.isPillarRound = isPillarRound

  def update_distance(self, distance: float):
    """Update the total distance traveled."""
    self.total_distance = distance
  
  def update_angle(self, angle: float):
    """Update the total angle turned."""
    self.total_angle = angle

  def transitionState(self, new_state: str):
    """Transition to a new state and reset the last state distance."""
    self.current_state = new_state
    self.last_state_distance = self.total_distance
    self.last_state_angle = self.total_angle
    print(f"Transitioned to state: {new_state}, total distance: {self.total_distance}, total angle: {self.total_angle}")

  def scheduleStateTransition(self, new_state: str, method: str, diff: float):
    """Schedule a state transition based on distance."""
    self._scheduled_state = new_state
    if method == "distance":
      self._scheduled_state_distance = self.total_distance + diff
    elif method == "angle":
      self._scheduled_state_angle = self.total_angle + diff
    else:
      raise ValueError("Invalid method for scheduling state transition. Use 'distance' or 'angle'.")

  def shouldTransitionState(self, portion_orange: float, portion_blue: float, pillars: list[Pillar]):
    """Determine if the state should transition based on distance and other conditions."""
    diff_distance = self.total_distance - self.last_state_distance
    self.diff_distance = diff_distance
    
    diff_angle = self.total_angle - self.last_state_angle
    self.diff_angle = diff_angle

    # Handle scheduled state transitions
    if self._scheduled_state is not None:
      if self._scheduled_state_distance is not None and self.total_distance >= self._scheduled_state_distance:
        print(f"Scheduled state transition to {self._scheduled_state}")
        self.transitionState(self._scheduled_state)
        self._scheduled_state = None
        self._scheduled_state_distance = None
        return True
      elif self._scheduled_state_angle is not None and self.total_angle >= self._scheduled_state_angle:
        print(f"Scheduled state transition to {self._scheduled_state}")
        self.transitionState(self._scheduled_state)
        self._scheduled_state = None
        self._scheduled_state_angle = None
        return True

    # STARTING state: Wait until the robot determines its direction
    if self.current_state == "STARTING":
      if abs(self.round_dir) > 5:
        self.round_dir = 1 if self.round_dir > 0 else -1
        self.search_for_dir = False
        print(f"Round direction determined: {'clockwise' if self.round_dir == 1 else 'counter-clockwise'}")
        self.transitionState("UNPARKING-1")
        return True
      else:
        self.search_for_dir = True
        return False

    if "UNPARKING" in self.current_state:
      if self.current_state == "UNPARKING-1":
        if abs(self.diff_angle) > 10:
          self.transitionState("UNPARKING-2")
          return True
      if self.current_state == "UNPARKING-2":
        if abs(self.diff_angle) > 65:
          self.transitionState("UNPARKING-3")
          return True
      if self.current_state == "UNPARKING-3":
        if self.diff_distance < -0:
          self.transitionState("UNPARKING-4")
          return True
      if self.current_state == "UNPARKING-4":
        if abs(diff_angle) > 80:
          self.transitionState("PD-CENTER") # todo have to ignore markers of the parking lot
          return True
      

    # PD-CENTER state: Transition to DONE if no turns are left
    if self.current_state == "PD-CENTER" and self.turns_left <= 0 and self._scheduled_state is None:
      self.scheduleStateTransition("DONE", "distance", 700.0)  # Transition after 50 mm
      return True

    # Hold the current state for a minimum distance
    HOLD_STATES = ["PD-CENTER"]
    if self.current_state in HOLD_STATES and diff_distance < 100.0:  # Hold for 100 mm
      return False
  
    # Handle pillar tracking and avoidance
    if len(pillars) > 0 and self.isPillarRound and self._allow_pillar_detection:
      next_pillar = pillars[0]
      if self.current_state == "PD-CENTER":
        self.next_pillar = next_pillar
        if next_pillar.height * next_pillar.width > 190 and not next_pillar.ignore:
          self.transitionState("TRACKING-PILLAR")
          return True
      elif self.current_state == "TRACKING-PILLAR":
        if next_pillar.height * next_pillar.width > 900 or next_pillar.y >= 180: # todo: check when last time pillar was avoided
          self.transitionState(f"AVOIDING-{'R' if next_pillar.color == 'RED' else 'G'}-1")
          print(f"transitioning reason area: {next_pillar.height * next_pillar.width > 530} or y: {next_pillar.y >= 200}")
          self.next_pillar = None
          return True

    # Handle lost pillar
    if self.current_state == "TRACKING-PILLAR" and len(pillars) == 0:
      print("Lost the pillar, tracking aborted")
      self.transitionState("PD-CENTER")
      return True

    # Handle avoiding states
    if self.current_state in ["AVOIDING-R-1", "AVOIDING-G-1", "AVOIDING-R-2", "AVOIDING-G-2"]:
      if self.current_state in ["AVOIDING-R-1", "AVOIDING-G-1"]:
        if abs(diff_angle) > 35:
          self.transitionState(self.current_state.replace("-1", "-2"))
          return True
      elif self.current_state in ["AVOIDING-R-2", "AVOIDING-G-2"]:
        if abs(diff_angle) < 5:
          self.transitionState("PD-CENTER")
          return True

    # Handle turning states
    TURNING_ANGLE = 85.0  # Angle threshold for turning
    if self.current_state in ["TURNING-REVERSE-L", "TURNING-REVERSE-R"]:
      if abs(self.diff_angle) > TURNING_ANGLE:
        self.transitionState("REVERSE-EXTRA")
        return True
      return False
    
    if self.current_state == "REVERSE-EXTRA":
      if self.diff_distance < -100:
        self.transitionState("PD-CENTER")
        return True
      return False

    # Handle turn markers
    MIN_PORTION = 0.15
    EXTRA_DISTANCE = 600.0  # Extra distance to move before reversing
    EXTRA_DISTANCE_PICTURE = 200.0  # Extra distance to move before taking a picture
    if portion_blue > MIN_PORTION:
      if self.current_state != "TURNING-REVERSE-R" and self.round_dir < 0 and self._scheduled_state is None:
        self.turns_left -= 1
        self._took_picture = False
        self.scheduleStateTransition("TURNING-REVERSE-L", "distance", EXTRA_DISTANCE)  # Turn left in 100 mm
      elif self.round_dir > 0 and self._scheduled_state is None and not self._took_picture:
        self.take_picture = True
        self._took_picture = True
        self.distance_take_picture = self.total_distance + EXTRA_DISTANCE_PICTURE
      elif "UNPARKING" not in self.current_state and self.current_state != "PD-CENTER":
        self.transitionState("PD-CENTER")
      else:
        return False
      return True

    if portion_orange > MIN_PORTION:
      if self.current_state != "TURNING-REVERSE-L" and self.round_dir > 0 and self._scheduled_state is None:
        self.turns_left -= 1
        self._took_picture = False
        self.scheduleStateTransition("TURNING-REVERSE-R", "distance", EXTRA_DISTANCE)  # Turn right in 100 mm
      elif self.round_dir < 0 and self._scheduled_state is None and not self._took_picture:
        self.take_picture = True
        self._took_picture = True
        self.distance_take_picture = self.total_distance + EXTRA_DISTANCE_PICTURE
      elif "UNPARKING" not in self.current_state and self.current_state != "PD-CENTER":
        self.transitionState("PD-CENTER")
      else:
        return False
      return True
    return False