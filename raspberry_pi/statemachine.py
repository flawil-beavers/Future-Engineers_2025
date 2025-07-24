from fftime import time
from helpers import Pillar

class StateMachine:

  current_state = "STARTING"
  round_dir = 0
  turns_left = 12

  last_state_distance = 0.0  # Distance at the last state transition
  total_distance = 0.0  # Total distance traveled
  diff_distance = 0.0  # Difference in distance for state transition
  _last_distance = 0.0  # Last distance traveled
  
  last_state_angle = 0.0  # Angle at the last state transition
  total_angle = 0.0  # Total angle turned
  diff_angle = 0.0  # Difference in angle for state transition
  diff_angle_0 = None  # Difference angle at the beginning of just driving straight
  
  following_angle = False  # True if the robot is following an angle
  
  distance_front = 0.0  # Distance to the black wall in front of the robot
  
  search_for_dir = True

  side = None # Side of the wall that the robot is following

  _scheduled_state = None
  _scheduled_state_distance = None  # Distance for scheduled state transition
  _scheduled_state_angle = None  # Angle for scheduled state transition
  _scheduled_state_reset_angle = True
  _allow_pillar_detection = True

  take_picture = False
  distance_take_picture = 0.0
  _took_picture = False
  
  pillar_driving_pos = [0, 0]

  next_pillar = None
  first_line_found = None

  avoid_big = False

  def __init__(self, isPillarRound: bool = False) -> None:
    self.last_state_distance = 0.0
    self.isPillarRound = isPillarRound

  def update_distance(self, distance: float):
    """Update the total distance traveled."""
    self._last_distance = self.diff_distance
    self.total_distance = distance
    self.diff_distance = self.total_distance - self.last_state_distance
  
  def update_angle(self, angle: float):
    """Update the total angle turned."""
    self.total_angle = angle
    self.diff_angle = self.total_angle - self.last_state_angle

  def determineSide(self):
    """Determine the side of the wall that the robot is following."""
    if ("AVOID-L" in self.current_state and self.round_dir == -1) or ("AVOID-R" in self.current_state and self.round_dir == 1):
      self.side = "INNER"
      self.side += "-L" if self.round_dir == -1 else "-R"
    elif ("AVOID-L" in self.current_state and self.round_dir == 1) or ("AVOID-R" in self.current_state and self.round_dir == -1):
      self.side = "OUTER"
      self.side += "-L" if self.round_dir == 1 else "-R"
    elif self.current_state == "PD-CENTER-2":
      self.side = "MIDDLE"
      self.side += "-L" if self.round_dir == 1 else "-R"
    else:
      self.side = None

  def transitionState(self, new_state: str, reset_angle: bool = True):
    """Transition to a new state and reset the last state distance."""
    self.current_state = new_state
    self.last_state_distance = self.total_distance
    if reset_angle:
      self.last_state_angle = self.total_angle
    self.following_angle = False
    self.determineSide()
    print(f"Transitioning to state: {new_state}")

  def scheduleStateTransition(self, new_state: str, method: str, diff: float, reset_angle: bool = True):
    """Schedule a state transition based on distance."""
    self._scheduled_state = new_state
    self._scheduled_state_reset_angle = reset_angle
    if method == "distance":
      self._scheduled_state_distance = self.total_distance + diff
    elif method == "angle":
      self._scheduled_state_angle = self.total_angle + diff
    else:
      raise ValueError("Invalid method for scheduling state transition. Use 'distance' or 'angle'.")
    
  def shouldTransitionState(self, portion_orange: float = 0, portion_blue: float = 0) -> bool:
    """Determine if the state should transition based on distance and other conditions."""

    pillars_same = self.pillar_driving_pos[0] == self.pillar_driving_pos[1] # are both pillars in the current section same?
    if self.round_dir == 1: # clockwise
      inner_colour = "RED"
    else: # counter-clockwise
      inner_colour = "GREEN"

    # Handle scheduled state transitions
    if self._scheduled_state is not None:
      if self._scheduled_state_distance is not None and self.total_distance >= self._scheduled_state_distance:
        print(f"Scheduled state transition to {self._scheduled_state}")
        self.transitionState(self._scheduled_state, reset_angle=self._scheduled_state_reset_angle)
        self._scheduled_state = None
        self._scheduled_state_distance = None
        return True
      elif self._scheduled_state_angle is not None and self.total_angle >= self._scheduled_state_angle:
        print(f"Scheduled state transition to {self._scheduled_state}")
        self.transitionState(self._scheduled_state, reset_angle=self._scheduled_state_reset_angle)
        self._scheduled_state = None
        self._scheduled_state_angle = None
        return True
    
    # STARTING state: Wait until the robot determines its direction
    if self.current_state == "STARTING":
      if abs(self.round_dir) > 5:
        self.round_dir = 1 if self.round_dir > 0 else -1
        self.search_for_dir = False
        print(f"Round direction determined: {'clockwise' if self.round_dir == 1 else 'counter-clockwise'}")
        if self.isPillarRound:
          self.transitionState("UNPARKING-1")
        else:
          self.transitionState("PD-CENTER")
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
        if self.diff_distance < 0:
          self.transitionState("UNPARKING-4")
          return True
      if self.current_state == "UNPARKING-4":
        if abs(self.diff_angle) > 80:
          self.transitionState("PD-CENTER-START")
          self.take_picture = True
          self.distance_take_picture = self.total_distance
          return True

    # PD-CENTER state: Transition to DONE if no turns are left
    if "PD-CENTER-2" in self.current_state and self.turns_left <= 0 and self._scheduled_state is None:
      self.scheduleStateTransition("DONE", "distance", 1200.0)  # Transition after 50 mm
      return True

    if self.current_state == "PD-CENTER" and self.turns_left <= 0 and self._scheduled_state is None:
      self.scheduleStateTransition("DONE", "distance", 700.0)  # Transition after 50 mm
      return True

    # Hold the current state for a minimum distance
    HOLD_STATES = ["PD-CENTER"]
    if self.current_state in HOLD_STATES and self.diff_distance < 100.0:  # Hold for 100 mm
      return False

    # Handle pillar avoidance after determining their position
    if self.current_state == "PD-CENTER-2" and self._took_picture:
      self._took_picture = False
      if self.turns_left % 4 == 0: # at the parking place
        if self.pillar_driving_pos[0] == inner_colour:
          self.transitionState(f"TURN-{'R' if inner_colour == 'RED' else 'L'}-1") # todo probably wrong state
        else:
          self.transitionState(f"PD-CENTER-PARKING-1")
        return True
      if self.pillar_driving_pos[0] == "RED":
        self.transitionState("TURN-R-1")
        return True
      elif self.pillar_driving_pos[0] == "GREEN":
        self.transitionState("TURN-L-1")
        return True
      else: # ! error
        raise ValueError(f"Unexpected pillar driving position: {self.pillar_driving_pos[0]}. Expected 'RED' or 'GREEN'.")

    DOUBLE_TURN_ANGLE = 65

    if "TURN-" in self.current_state and "-1" in self.current_state and abs(self.diff_angle) > DOUBLE_TURN_ANGLE:
      self.transitionState(self.current_state.replace("-1", "-2"))
      return True

    if "TURN-" in self.current_state and "-2" in self.current_state and abs(self.diff_angle) > DOUBLE_TURN_ANGLE:
      self.transitionState(self.current_state.replace("TURN-", "AVOID-").replace("-2", "-1"))
      return True
    
    # Handle side changing if pillars are not the same in front and back
    if self.current_state in ["AVOID-L-1", "AVOID-R-1", "PD-CENTER-PARKING-1"]:
      if not pillars_same:
        if self.turns_left % 4 == 0 and self.diff_distance > 500: # at the parking place
          if self.pillar_driving_pos[1] == inner_colour:
            self.transitionState(f"AVOID-{'R' if inner_colour == 'RED' else 'L'}-2")
          else:
            self.transitionState(f"PD-CENTER-PARKING-2")
          return True
        if self.current_state == "AVOID-R-1":
          self.transitionState("AVOID-L-2", reset_angle=False)
          return True
        elif self.current_state == "AVOID-L-1":
          self.transitionState("AVOID-R-2", reset_angle=False)
          return True
      else:
        self.transitionState(self.current_state.replace("-1", "-2"))
        return True
    
    # Handle finishing avoidance
    if self.current_state in ["AVOID-L-2", "AVOID-R-2"] and ((self.diff_distance > 1200 and not pillars_same) or (self.diff_distance > 900 and pillars_same)) and self.distance_front > 0.22:
      # if avoiding on the right turn left first and then right otherwise other way round
      self.transitionState("TURN-R-3" if self.current_state == "AVOID-L-2" else "TURN-L-3")
      return True
    
    # Handle return to middle after avoiding pillars
    if "TURN-" in self.current_state and "-3" in self.current_state and abs(self.diff_angle) > DOUBLE_TURN_ANGLE:
      self.transitionState(self.current_state.replace("-3", "-4"))
      return True

    if "TURN-" in self.current_state and "-4" in self.current_state and abs(self.diff_angle) > DOUBLE_TURN_ANGLE:
      self.transitionState("GYRO")
      return True
    
    DISTANCE_TO_WALL = 0.95  # Distance to the wall for state transition
    
    if self.current_state == "GYRO" and self.distance_front > DISTANCE_TO_WALL:
      self.transitionState("TURNING-REVERSE-R" if self.round_dir > 0 else "TURNING-REVERSE-L")
      return True
    
    if self.current_state == "PD-CENTER-START" and self._took_picture:
      for i in range(2):
        if self.pillar_driving_pos[i] == inner_colour:
          self.transitionState(f"TURN-{'R' if inner_colour == 'RED' else 'L'}-5")
          self._took_picture = False
          return True
    
    if "TURN-" in self.current_state and "-5" in self.current_state and abs(self.diff_angle) > DOUBLE_TURN_ANGLE:
      self.transitionState(self.current_state.replace("-5", "-6"), reset_angle=False)
      return True
    
    if "TURN-" in self.current_state and "-6" in self.current_state and abs(self.diff_angle) < 20:
      self.transitionState(f"AVOID-{self.current_state[5]}-3", reset_angle=False)
      return True
    
    if "AVOID-" in self.current_state and "-3" in self.current_state and self.following_angle:
      if abs(self.diff_angle) < 20: # todo: Maybe unnecessary
        self.transitionState(self.current_state.replace("-3", "-4"), reset_angle=False)
        self.scheduleStateTransition(f"TURN-{'L' if self.current_state[6] == 'R' else 'R'}-3", "distance", 200.0)
        # self.transitionState(f"TURN-{'L' if self.current_state[6] == 'R' else 'R'}-3", reset_angle=False)
        return True
      return False
    
    if self.current_state in ["PD-CENTER-START", "PD-CENTER-PARKING-2"] and self.diff_distance > 200 and self.distance_front > 0.4:
      self.transitionState("GYRO")
      self._took_picture = False
      return True

    # Handle turning states
    TURNING_ANGLE = 85.0  # Angle threshold for turning
    if self.current_state in ["TURNING-REVERSE-L", "TURNING-REVERSE-R"]:
      if abs(self.diff_angle) > TURNING_ANGLE:
        self.transitionState("REVERSE-EXTRA")
        self.turns_left -= 1
        return True
      return False
    
    if self.current_state == "REVERSE-EXTRA":
      if self.diff_distance < -300 or abs(self.diff_distance - self._last_distance) < 1:
        if abs(self.diff_distance - self._last_distance) < 1:
          print(f"Diff distance is lower then 1 mm: abs({self.diff_distance} - {self._last_distance}) = {abs(self.diff_distance - self._last_distance)}, total distance: {self.total_distance}")
        self.transitionState("PD-CENTER-2")
        if self.turns_left >= 8:
          self.take_picture = True
        self.distance_take_picture = self.total_distance + 490
        return True
      return False

    if not self.isPillarRound:
      # Handle turning states
      TURNING_ANGLE = 70.0  # Angle threshold for turning
      if self.current_state in ["TURNING-L", "TURNING-R"]:
        if abs(self.diff_angle) > TURNING_ANGLE:
          self.transitionState("PD-CENTER")
          return True
        return False

      # Handle turn markers
      MIN_PORTION = 0.15
      if portion_blue > MIN_PORTION:
        if self.current_state != "TURNING-R" and self.round_dir < 0 and self._scheduled_state is None:
          self.turns_left -= 1
          self.scheduleStateTransition("TURNING-L", "distance", 100.0)  # Turn left in 100 mm
        elif self.current_state != "PD-CENTER":
          self.transitionState("PD-CENTER")
        else:
          return False
        return True

      if portion_orange > MIN_PORTION:
        if self.current_state != "TURNING-L" and self.round_dir > 0 and self._scheduled_state is None:
          self.turns_left -= 1
          self.scheduleStateTransition("TURNING-R", "distance", 100.0)  # Turn right in 100 mm
        elif self.current_state != "PD-CENTER":
          self.transitionState("PD-CENTER")
        else:
          return False
        return True
      return False
    
    return False