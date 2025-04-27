from fftime import time
from helpers import Pillar

class StateMachine:

  current_state = "STARTING"
  last_state_distance = 0.0  # Distance at the last state transition
  round_dir = 0
  turns_left = 12

  total_distance = 0.0  # Total distance traveled
  distance_diff = 0.0  # Difference in distance for state transition
  
  search_for_dir = True

  _scheduled_state = None
  _scheduled_state_distance = 0.0  # Distance for scheduled state transition

  next_pillar = None
  first_line_found = None

  avoid_big = False

  def __init__(self, isPillarRound: bool = False) -> None:
    self.last_state_distance = 0.0
    self.isPillarRound = isPillarRound

  def update_distance(self, distance: float):
    """Update the total distance traveled."""
    self.total_distance = distance

  def transitionState(self, new_state: str):
    """Transition to a new state and reset the last state distance."""
    self.current_state = new_state
    self.last_state_distance = self.total_distance
    print(f"Transitioned to state: {new_state}, total distance: {self.total_distance}")

  def scheduleStateTransition(self, new_state: str, distance_diff: float):
    """Schedule a state transition based on distance."""
    self._scheduled_state = new_state
    self._scheduled_state_distance = self.total_distance + distance_diff

  def shouldTransitionState(self, portion_orange: float, portion_blue: float, pillars: list[Pillar]):
    """Determine if the state should transition based on distance and other conditions."""
    distance_diff = self.total_distance - self.last_state_distance
    self.distance_diff = distance_diff

    # Handle scheduled state transitions
    if self._scheduled_state is not None:
      if self.total_distance >= self._scheduled_state_distance:
        print(f"Scheduled state transition to {self._scheduled_state}")
        self.transitionState(self._scheduled_state)
        self._scheduled_state = None
        return True

    # STARTING state: Wait until the robot determines its direction
    if self.current_state == "STARTING":
      if abs(self.round_dir) > 10:
        self.round_dir = 1 if self.round_dir > 0 else -1
        self.search_for_dir = False
        self.transitionState("PD-CENTER")
        return True
      else:
        self.search_for_dir = True
        return False

    # PD-CENTER state: Transition to DONE if no turns are left
    if self.current_state == "PD-CENTER" and self.turns_left <= 0 and self._scheduled_state is None:
      self.scheduleStateTransition("DONE", 50.0)  # Transition after 50 units of distance
      return True

    # Hold the current state for a minimum distance
    HOLD_STATES = ["TURNING-L", "TURNING-R", "PD-CENTER"]
    if self.current_state in HOLD_STATES and distance_diff < 100.0:  # Hold for 10 units of distance
      return False

    # Handle pillar tracking and avoidance
    if len(pillars) > 0 and self.isPillarRound:
      next_pillar = pillars[0]
      if self.current_state == "PD-CENTER":
        self.next_pillar = next_pillar
        if next_pillar.height * next_pillar.width > 190 and not next_pillar.ignore:
          self.transitionState("TRACKING-PILLAR")
          return True
      elif self.current_state == "TRACKING-PILLAR":
        if next_pillar.height * next_pillar.width > 530: # todo: check when last time pillar was avoided
          self.transitionState(f"AVOIDING-{'R' if next_pillar.color == 'RED' else 'G'}")
          self.next_pillar = None
          return True

    # Handle lost pillar
    if self.current_state == "TRACKING-PILLAR" and len(pillars) == 0:
      print("Lost the pillar, tracking aborted")
      self.transitionState("PD-CENTER")
      return True

    # Handle avoiding states
    if self.current_state in ["AVOIDING-R", "AVOIDING-G"]:
      if distance_diff > 20.0:  # Avoid for 20 units of distance
        self.transitionState("PD-CENTER") # todo add last avoid distance
        self.avoid_big = False # probabyl unnecessary
        return True

    # Handle turning states
    TURN_DISTANCE = 15.0
    if self.current_state in ["TURNING-L", "TURNING-R"]:
      if distance_diff > TURN_DISTANCE:
        self.transitionState("PD-CENTER")
        return True
      return False

    # Handle turn markers
    MIN_PORTION = 0.25
    if portion_blue > MIN_PORTION:
      if self.current_state != "TURNING-R" and self.round_dir < 0:
        self.turns_left -= 1
        self.scheduleStateTransition("TURNING-L", 15.0)  # Turn left for 15 units of distance
      elif self.current_state != "PD-CENTER":
        self.transitionState("PD-CENTER")
      else:
        return False
      return True

    if portion_orange > MIN_PORTION:
      if self.current_state != "TURNING-L" and self.round_dir > 0:
        self.turns_left -= 1
        self.scheduleStateTransition("TURNING-R", 15.0)  # Turn right for 15 units of distance
      elif self.current_state != "PD-CENTER":
        self.transitionState("PD-CENTER")
      else:
        return False
      return True

    return False